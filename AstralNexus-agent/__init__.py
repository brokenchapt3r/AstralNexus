"""AstralNexus - 天枢：面向超长程复杂任务的动态异构群体智能协同推理引擎."""

from __future__ import annotations

# Version information
from AstralNexus.core.identity import VERSION

__version__ = VERSION
__author__ = "AstralNexus Team"
__description__ = "面向超长程复杂任务的动态异构群体智能协同推理引擎"

# Core components
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

# Runtime components
from AstralNexus.runtime.context import ContextManager
from AstralNexus.runtime.model_client import ModelClient
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.types import RuntimeCapability, RuntimeRequest, RuntimeResult
from AstralNexus.runtime.workspace import WorkspaceBoundary
from AstralNexus.tools.executor import ToolExecutor

# Storage components
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

# Initialize the role registry
initialize_role_registry()

# Convenience exports
__all__ = [
    # Core components
    "AgentRole",
    "AgentRegistry",
    "get_agent_registry",
    "EvaluationMetrics",
    "EvaluationReporter",
    "EvaluationResult",
    "Fault",
    "FaultDetector",
    "ImpactAnalyzer",
    "RecoveryExecutor",
    "CODE_NAME",
    "HOME_ENV",
    "PRODUCT_NAME",
    "bootstrap_environment",
    "MemoryEntry",
    "MemoryFederation",
    "MessageProtocol",
    "StructuredMessage",
    "Orchestrator",
    "Resource",
    "ResourcePool",
    "ResourceScheduler",
    "ResourceTier",
    "get_role_capabilities",
    "initialize_role_registry",
    "Router",
    "VALID_TRANSITIONS",
    "assert_transition",
    "can_transition",
    "Artifact",
    "Fact",
    "Metric",
    "TaskEdge",
    "TaskGraph",
    "TaskNode",
    "new_run_dir",
    "run_root",
    "write_json",
    
    # Runtime components
    "ContextManager",
    "ModelClient",
    "ToolExecutor",
    "TraceRecorder",
    "RuntimeCapability",
    "RuntimeRequest",
    "RuntimeResult",
    "WorkspaceBoundary",
    
    # Storage components
    "SQLiteTaskGraphStore",
]
