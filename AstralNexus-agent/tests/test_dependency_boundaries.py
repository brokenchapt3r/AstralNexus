import json
import os
from pathlib import Path
import subprocess
import sys
from AstralNexus.core.agent_registry import AgentRegistry, AgentRole, agent_registry_scope
from AstralNexus.tools.executor import ToolExecutor


def test_type_imports_do_not_boot_governor_or_agent_implementations():
    source = '''import sys,json
from AstralNexus.core.governance_types import TaskGraphPatch,EventType
from AstralNexus.agents.contracts import AgentContext
print(json.dumps({"governor": "AstralNexus.core.governor" in sys.modules,
"base": "AstralNexus.agents.base" in sys.modules}))
'''
    result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'governor': False, 'base': False}


def test_legacy_public_types_are_the_same_classes():
    from AstralNexus.core import governor, governance_types
    from AstralNexus.agents import base, contracts
    for name in ['EventType', 'TaskGraphPatch', 'AdmissionDecision', 'WorkerMatcherTransientError', 'WorkerMatcherProfileError']:
        assert getattr(governor, name) is getattr(governance_types, name)
    for name in ['AgentContext', 'AgentResponse', 'ExecutionOutcome']:
        assert getattr(base, name) is getattr(contracts, name)


def test_catalogue_clone_and_executor_capture_isolate_later_mutation(tmp_path):
    registry = AgentRegistry()
    registry.register_role(AgentRole('custom', 'custom', default_tools=['file_read']))
    isolated = registry.clone()
    with agent_registry_scope(isolated):
        executor = ToolExecutor(workspace_root=tmp_path)
    registry.get_role('custom').default_tools.append('file_write')
    assert isolated.get_role('custom').default_tools == ['file_read']
    assert executor.agent_catalog is isolated


def test_orchestrators_do_not_share_mutable_role_catalogues(tmp_path):
    from AstralNexus.core.orchestrator import Orchestrator
    with Orchestrator(tmp_path / 'one') as first, Orchestrator(tmp_path / 'two') as second:
        assert first.agent_registry is not second.agent_registry
        first.agent_registry.register_role(AgentRole('one-only', 'one'))
        assert second.agent_registry.get_role('one-only') is None
