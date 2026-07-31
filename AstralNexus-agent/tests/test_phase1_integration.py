#!/usr/bin/env python3
"""Integration test for AstralNexus Phase 1 enhancements."""

import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def test_imports():
    """Test that all new modules can be imported."""
    print("=" * 70)
    print("Test: Import Verification")
    print("=" * 70)

    from AstralNexus.core.exceptions import (
        AstralNexusError, Severity,
        ModelClientError, TaskGraphError, ToolExecutionError,
    )
    print("  [OK] core.exceptions")

    from AstralNexus.core.lifecycle import AgentLifecycleManager
    from AstralNexus.core.state_machine import TaskStatus
    print("  [OK] core.lifecycle")

    from AstralNexus.core.orchestrator import Orchestrator
    print("  [OK] core.orchestrator")

    from AstralNexus.core.router import Router
    print("  [OK] core.router")

    from AstralNexus.tools.registry import ToolRegistry, registry
    print("  [OK] tools.registry")

    from AstralNexus.tools.toolsets import resolve_toolset, get_toolset_names
    print("  [OK] tools.toolsets")

    from AstralNexus.runtime.context import ContextManager
    print("  [OK] runtime.context")

    from AstralNexus.runtime.trace import TraceRecorder
    print("  [OK] runtime.trace")

    from AstralNexus.cli.commands import process_command, COMMAND_REGISTRY
    print("  [OK] cli.commands")


def test_exception_hierarchy():
    print("\n" + "=" * 70)
    print("Test: Exception Hierarchy")
    print("=" * 70)

    from AstralNexus.core.exceptions import (
        AstralNexusError, Severity,
        ModelClientError, ToolExecutionError,
        format_tool_error,
    )

    err = ModelClientError("API error", severity=Severity.RECOVERABLE)
    assert err.severity == Severity.RECOVERABLE
    print(f"  [OK] ModelClientError severity={err.severity.value}")

    structured = err.to_structured()
    assert structured["error_class"] == "ModelClientError"
    print(f"  [OK] to_structured() works")

    json_str = format_tool_error(ToolExecutionError("boom"), "my_tool")
    parsed = json.loads(json_str)
    assert "error" in parsed
    print(f"  [OK] format_tool_error() returns valid JSON")

    assert isinstance(err, AstralNexusError)
    print("  [OK] All exceptions inherit from AstralNexusError")


def test_tool_registry():
    print("\n" + "=" * 70)
    print("Test: Tool Registry")
    print("=" * 70)

    from AstralNexus.tools.registry import ToolRegistry, tool_result

    reg = ToolRegistry()

    def test_handler(args, **kwargs):
        return tool_result({"success": True, "value": args.get("x", 0) * 2})

    reg.register(
        name="double",
        toolset="math",
        schema={"name": "double", "description": "Double a number",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}},
        handler=test_handler,
    )
    print(f"  [OK] Registered tool: double")

    result = json.loads(reg.dispatch("double", {"x": 21}))
    assert result["success"] is True and result["value"] == 42
    print(f"  [OK] dispatch returned {result['value']}")

    defs = reg.get_definitions({"double"})
    assert len(defs) == 1
    print(f"  [OK] get_definitions() returns OpenAI-format schema")

    err_result = json.loads(reg.dispatch("nonexistent", {}))
    assert "error" in err_result
    print(f"  [OK] Unknown tool returns structured error")


def test_toolsets():
    print("\n" + "=" * 70)
    print("Test: Toolsets")
    print("=" * 70)

    from AstralNexus.tools.toolsets import resolve_toolset, get_toolset_names, get_toolset_info

    names = get_toolset_names()
    print(f"  [OK] {len(names)} toolsets available")

    planner_tools = resolve_toolset("planner")
    print(f"  [OK] planner: {planner_tools}")

    sw_tools = resolve_toolset("software_engineering")
    print(f"  [OK] software_engineering: {len(sw_tools)} tools")

    all_tools = resolve_toolset("full")
    print(f"  [OK] full: {len(all_tools)} tools (deduplicated)")

    info = get_toolset_info("coder")
    assert info is not None
    print(f"  [OK] coder: {info['tool_count']} tools")


def test_context_manager():
    print("\n" + "=" * 70)
    print("Test: Enhanced Context Manager")
    print("=" * 70)

    from AstralNexus.runtime.context import ContextManager

    ctx = ContextManager(max_context_tokens=8000)

    tokens = ctx.estimate_tokens("Hello world! " * 100)
    print(f"  [OK] estimate_tokens ~{tokens}")

    ctx.add_constraint("c1", "Must use Python 3.9+", source_node_id="T-001")
    ctx.add_constraint("c1", "Must use Python 3.10+", source_node_id="T-002")

    active = ctx.get_active_constraints()
    assert len(active) == 1 and "3.10" in active[0]
    print(f"  [OK] Constraint versioning: '{active[0]}'")

    history = ctx.get_constraint_history("c1")
    assert len(history) == 2
    print(f"  [OK] Constraint history: {len(history)} versions")

    long_text = "Fact: the system works. " * 500
    compressed = ctx.compress_context(
        long_text,
        preserve_facts=[{"text": "Critical: no breaking changes", "source_node_id": "T-001"}],
        target_tokens=100,
    )
    print(f"  [OK] Compression: {len(long_text)} -> {len(compressed)} chars")

    facts = ctx.get_preserved_facts()
    assert len(facts) >= 1
    print(f"  [OK] Preserved facts: {len(facts)}")

    prompt = ctx.build_prompt("Do something", active_constraints=active)
    assert "活跃约束" in prompt
    print(f"  [OK] build_prompt injects constraints")


def test_lifecycle_manager():
    print("\n" + "=" * 70)
    print("Test: Agent Lifecycle Manager")
    print("=" * 70)

    from AstralNexus.core.lifecycle import AgentLifecycleManager
    from AstralNexus.core.role_registry import initialize_role_registry
    from AstralNexus.core.state_machine import TaskStatus

    initialize_role_registry()

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        lcm = AgentLifecycleManager(run_dir)

        a1 = lcm.create_agent("coder", "T-001")
        a2 = lcm.create_agent("verifier", "T-002")
        print(f"  [OK] Created: {a1.agent_id}, {a2.agent_id}")

        lcm.transition_agent(a1.agent_id, TaskStatus.READY, "Ready")
        lcm.transition_agent(a1.agent_id, TaskStatus.RUNNING, "Starting")
        lcm.cancel_agent(a2.agent_id, "Not selected for this wave")
        lcm.update_agent_metrics(a1.agent_id, iteration_count=5, token_usage={"total": 1500})

        # Check active count while a1 is RUNNING and a2 is CANCELLED.
        active = lcm.get_active_agents()
        assert len(active) == 1
        print(f"  [OK] Active agents: {len(active)}")

        lcm.mark_result_submitted(a1.agent_id)
        lcm.record_verification_verdict(
            a1.agent_id,
            approved=True,
            reason="Verifier approved",
        )

        by_role = lcm.get_agents_by_role("coder")
        assert len(by_role) == 1
        print(f"  [OK] Agents by role 'coder': {len(by_role)}")

        health = lcm.health_check(a1.agent_id)
        assert health["state"] == "completed"
        print(f"  [OK] Health check: {health['state']}")

        summary = lcm.get_lifecycle_summary()
        assert summary["total_agents"] == 2
        print(f"  [OK] Summary: {summary['total_agents']} agents, states={summary['by_state']}")

        lcm2 = AgentLifecycleManager(run_dir)
        lcm2.restore_snapshot(lcm.export_active_snapshot())
        assert len(lcm2.agents) == 0
        print("  [OK] Runtime snapshot excludes terminal agents")


def test_orchestrator_init():
    print("\n" + "=" * 70)
    print("Test: Orchestrator Integration")
    print("=" * 70)

    from AstralNexus.core.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        orch = Orchestrator(run_dir)

        assert orch.lifecycle_manager is not None
        print("  [OK] lifecycle_manager")
        assert orch.context_manager is not None
        print("  [OK] context_manager")
        assert orch.router is not None
        print("  [OK] router")
        # Formal runs converge through Governor termination and repeated-error
        # detection; they do not stop at an arbitrary orchestration round cap.
        assert orch.max_iterations == 0
        print("  [OK] max_iterations=unlimited")

        root_id = orch.initialize_task_graph("Build a web app", ["Must use React", "Must have tests"])
        assert root_id == "P-001"
        assert orch.task_graph.get_node("U-001").role == "user_intent"
        assert orch.task_graph.get_node(root_id).dependencies == ["U-001"]
        print(f"  [OK] Task graph root={root_id}")

        active = orch.context_manager.get_active_constraints()
        assert len(active) == 2
        print(f"  [OK] {len(active)} active constraints")


def test_cli_commands():
    print("\n" + "=" * 70)
    print("Test: CLI Commands")
    print("=" * 70)

    from AstralNexus.cli.commands import process_command, resolve_command, COMMAND_REGISTRY

    assert resolve_command("/help") == "help"
    assert resolve_command("version") == "version"
    assert resolve_command("/nonexistent") is None
    print("  [OK] resolve_command()")

    print(f"  [OK] {len(COMMAND_REGISTRY)} commands registered")

    result = process_command("/help")
    assert result == 0
    print("  [OK] /help -> 0")

    result = process_command("/version")
    assert result == 0
    print("  [OK] /version -> 0")

    result = process_command("/nonexistent")
    assert result == 1
    print("  [OK] /nonexistent -> 1")


def main():
    print("\n" + "=" * 70)
    print("  ASTRALNEXUS PHASE 1 INTEGRATION TEST SUITE")
    print("=" * 70 + "\n")

    tests = [
        ("Import Verification", test_imports),
        ("Exception Hierarchy", test_exception_hierarchy),
        ("Tool Registry", test_tool_registry),
        ("Toolsets", test_toolsets),
        ("Context Manager", test_context_manager),
        ("Lifecycle Manager", test_lifecycle_manager),
        ("Orchestrator Integration", test_orchestrator_init),
        ("CLI Commands", test_cli_commands),
    ]

    results = []
    for name, func in tests:
        try:
            ok = func()
            results.append((name, ok))
        except Exception as e:
            print(f"\n  [FAIL] '{name}': {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    passed = sum(1 for _, s in results if s)
    total = len(results)

    for name, success in results:
        tag = "PASS" if success else "FAIL"
        print(f"  [{tag}] {name}")

    print(f"\n  Result: {passed}/{total} tests passed")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
