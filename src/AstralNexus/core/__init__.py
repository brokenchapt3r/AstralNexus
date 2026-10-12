"""Lazy public exports; importing a data type does not boot the runtime."""
from __future__ import annotations
from importlib import import_module
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from AstralNexus.core.exceptions import AstralNexusError, Severity
    from AstralNexus.core.lifecycle import AgentLifecycleManager
    from AstralNexus.core.state_machine import RunStatus, RuntimePhase, TaskStatus
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.router import Router
    from AstralNexus.core.task_graph import TaskGraph, TaskNode, TaskEdge

__all__ = ['AstralNexusError', 'Severity', 'AgentLifecycleManager', 'TaskStatus', 'RunStatus', 'RuntimePhase', 'Orchestrator', 'Router', 'TaskGraph', 'TaskNode', 'TaskEdge']

_EXPORTS = {'AstralNexusError': ('AstralNexus.core.exceptions', 'AstralNexusError'), 'Severity': ('AstralNexus.core.exceptions', 'Severity'), 'AgentLifecycleManager': ('AstralNexus.core.lifecycle', 'AgentLifecycleManager'), 'TaskStatus': ('AstralNexus.core.state_machine', 'TaskStatus'), 'RunStatus': ('AstralNexus.core.state_machine', 'RunStatus'), 'RuntimePhase': ('AstralNexus.core.state_machine', 'RuntimePhase'), 'Orchestrator': ('AstralNexus.core.orchestrator', 'Orchestrator'), 'Router': ('AstralNexus.core.router', 'Router'), 'TaskGraph': ('AstralNexus.core.task_graph', 'TaskGraph'), 'TaskNode': ('AstralNexus.core.task_graph', 'TaskNode'), 'TaskEdge': ('AstralNexus.core.task_graph', 'TaskEdge')}

def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
