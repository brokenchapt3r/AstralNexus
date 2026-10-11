"""Lazy public exports; importing a data type does not boot the runtime."""
from __future__ import annotations
from importlib import import_module
from typing import Any, TYPE_CHECKING

from AstralNexus.core.identity import VERSION
__version__ = VERSION
__author__ = "AstralNexus Team"
__description__ = "面向超长程复杂任务的动态异构群体智能协同推理引擎"

if TYPE_CHECKING:
    from AstralNexus.core.identity import VERSION
    from AstralNexus.core.agent_registry import AgentRole, AgentRegistry, get_agent_registry
    from AstralNexus.core.artifacts import new_run_dir, run_root, write_json
    from AstralNexus.core.evaluator import EvaluationMetrics, EvaluationReporter, EvaluationResult
    from AstralNexus.core.fault_recovery import Fault, FaultDetector, ImpactAnalyzer, RecoveryExecutor
    from AstralNexus.core.identity import CODE_NAME, HOME_ENV, PRODUCT_NAME, bootstrap_environment
    from AstralNexus.core.memory_federation import MemoryEntry, MemoryFederation
    from AstralNexus.core.message_protocol import MessageProtocol, StructuredMessage
    from AstralNexus.core.orchestrator import Orchestrator
    from AstralNexus.core.resource_scheduler import Resource, ResourcePool, ResourceScheduler, ResourceTier
    from AstralNexus.core.role_registry import (
        get_role_capabilities,
        initialize_role_registry,
    )
    from AstralNexus.core.router import Router
    from AstralNexus.core.state_machine import VALID_TRANSITIONS, assert_transition, can_transition
    from AstralNexus.core.task_graph import Artifact, Fact, Metric, TaskEdge, TaskGraph, TaskNode
    from AstralNexus.runtime.context import ContextManager
    from AstralNexus.runtime.model_client import ModelClient
    from AstralNexus.runtime.trace import TraceRecorder
    from AstralNexus.runtime.types import RuntimeCapability, RuntimeRequest, RuntimeResult
    from AstralNexus.runtime.workspace import WorkspaceBoundary
    from AstralNexus.tools.executor import ToolExecutor
    from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

__all__ = ['AgentRole', 'AgentRegistry', 'get_agent_registry', 'EvaluationMetrics', 'EvaluationReporter', 'EvaluationResult', 'Fault', 'FaultDetector', 'ImpactAnalyzer', 'RecoveryExecutor', 'CODE_NAME', 'HOME_ENV', 'PRODUCT_NAME', 'bootstrap_environment', 'MemoryEntry', 'MemoryFederation', 'MessageProtocol', 'StructuredMessage', 'Orchestrator', 'Resource', 'ResourcePool', 'ResourceScheduler', 'ResourceTier', 'get_role_capabilities', 'initialize_role_registry', 'Router', 'VALID_TRANSITIONS', 'assert_transition', 'can_transition', 'Artifact', 'Fact', 'Metric', 'TaskEdge', 'TaskGraph', 'TaskNode', 'new_run_dir', 'run_root', 'write_json', 'ContextManager', 'ModelClient', 'ToolExecutor', 'TraceRecorder', 'RuntimeCapability', 'RuntimeRequest', 'RuntimeResult', 'WorkspaceBoundary', 'SQLiteTaskGraphStore']

_EXPORTS = {'AgentRole': ('AstralNexus.core.agent_registry', 'AgentRole'), 'AgentRegistry': ('AstralNexus.core.agent_registry', 'AgentRegistry'), 'get_agent_registry': ('AstralNexus.core.agent_registry', 'get_agent_registry'), 'EvaluationMetrics': ('AstralNexus.core.evaluator', 'EvaluationMetrics'), 'EvaluationReporter': ('AstralNexus.core.evaluator', 'EvaluationReporter'), 'EvaluationResult': ('AstralNexus.core.evaluator', 'EvaluationResult'), 'Fault': ('AstralNexus.core.fault_recovery', 'Fault'), 'FaultDetector': ('AstralNexus.core.fault_recovery', 'FaultDetector'), 'ImpactAnalyzer': ('AstralNexus.core.fault_recovery', 'ImpactAnalyzer'), 'RecoveryExecutor': ('AstralNexus.core.fault_recovery', 'RecoveryExecutor'), 'CODE_NAME': ('AstralNexus.core.identity', 'CODE_NAME'), 'HOME_ENV': ('AstralNexus.core.identity', 'HOME_ENV'), 'PRODUCT_NAME': ('AstralNexus.core.identity', 'PRODUCT_NAME'), 'bootstrap_environment': ('AstralNexus.core.identity', 'bootstrap_environment'), 'MemoryEntry': ('AstralNexus.core.memory_federation', 'MemoryEntry'), 'MemoryFederation': ('AstralNexus.core.memory_federation', 'MemoryFederation'), 'MessageProtocol': ('AstralNexus.core.message_protocol', 'MessageProtocol'), 'StructuredMessage': ('AstralNexus.core.message_protocol', 'StructuredMessage'), 'Orchestrator': ('AstralNexus.core.orchestrator', 'Orchestrator'), 'Resource': ('AstralNexus.core.resource_scheduler', 'Resource'), 'ResourcePool': ('AstralNexus.core.resource_scheduler', 'ResourcePool'), 'ResourceScheduler': ('AstralNexus.core.resource_scheduler', 'ResourceScheduler'), 'ResourceTier': ('AstralNexus.core.resource_scheduler', 'ResourceTier'), 'get_role_capabilities': ('AstralNexus.core.role_registry', 'get_role_capabilities'), 'initialize_role_registry': ('AstralNexus.core.role_registry', 'initialize_role_registry'), 'Router': ('AstralNexus.core.router', 'Router'), 'VALID_TRANSITIONS': ('AstralNexus.core.state_machine', 'VALID_TRANSITIONS'), 'assert_transition': ('AstralNexus.core.state_machine', 'assert_transition'), 'can_transition': ('AstralNexus.core.state_machine', 'can_transition'), 'Artifact': ('AstralNexus.core.task_graph', 'Artifact'), 'Fact': ('AstralNexus.core.task_graph', 'Fact'), 'Metric': ('AstralNexus.core.task_graph', 'Metric'), 'TaskEdge': ('AstralNexus.core.task_graph', 'TaskEdge'), 'TaskGraph': ('AstralNexus.core.task_graph', 'TaskGraph'), 'TaskNode': ('AstralNexus.core.task_graph', 'TaskNode'), 'new_run_dir': ('AstralNexus.core.artifacts', 'new_run_dir'), 'run_root': ('AstralNexus.core.artifacts', 'run_root'), 'write_json': ('AstralNexus.core.artifacts', 'write_json'), 'ContextManager': ('AstralNexus.runtime.context', 'ContextManager'), 'ModelClient': ('AstralNexus.runtime.model_client', 'ModelClient'), 'ToolExecutor': ('AstralNexus.tools.executor', 'ToolExecutor'), 'TraceRecorder': ('AstralNexus.runtime.trace', 'TraceRecorder'), 'RuntimeCapability': ('AstralNexus.runtime.types', 'RuntimeCapability'), 'RuntimeRequest': ('AstralNexus.runtime.types', 'RuntimeRequest'), 'RuntimeResult': ('AstralNexus.runtime.types', 'RuntimeResult'), 'WorkspaceBoundary': ('AstralNexus.runtime.workspace', 'WorkspaceBoundary'), 'SQLiteTaskGraphStore': ('AstralNexus.storage.sqlite_store', 'SQLiteTaskGraphStore')}

def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
