from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.state_machine import normalize_status
from AstralNexus.core.evaluator import EvaluationReporter
from AstralNexus.runtime.trace import TraceRecorder

# Public API - these are the primary classes for fault recovery
__all__ = [
    "Fault",
    "FaultDetector",
    "ImpactAnalyzer",
    "RecoveryExecutor",
    "FaultRecoveryEngine",
]


class FaultStatus(str, Enum):
    DETECTED = "detected"
    ANALYZED = "analyzed"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    FAILED = "failed"


@dataclass
class Fault:
    """Represents a fault in the system."""
    
    # Fault identifier
    fault_id: str
    
    # Type of fault
    fault_type: str  # "requirement_change", "node_failure", "tool_failure", "memory_loss", "resource_degradation"
    
    # Target of the fault (node_id, resource_id, etc.)
    target: str
    
    # Payload with fault details
    payload: Dict[str, Any]
    
    # Timestamp when the fault occurred
    timestamp: str
    
    # Status of the fault
    status: FaultStatus = FaultStatus.DETECTED
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, FaultStatus):
            self.status = FaultStatus(str(self.status))


class FaultDetector:
    """Detects faults in the system."""
    
    def __init__(self, task_graph: TaskGraph, trace_recorder: TraceRecorder):
        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self.faults: List[Fault] = []
    
    def detect_requirement_change(self, node_id: str, new_constraints: List[str]) -> Fault:
        """Detect a requirement change fault.
        
        Args:
            node_id: The node affected by the requirement change
            new_constraints: The new constraints
            
        Returns:
            Detected fault
        """
        fault = Fault(
            fault_id=f"fault:req_change:{node_id}:{len(self.faults)}",
            fault_type="requirement_change",
            target=node_id,
            payload={"new_constraints": new_constraints},
            timestamp="",  # This would be set to current timestamp
            metadata={"detector": "FaultDetector"}
        )
        
        self.faults.append(fault)
        
        # Record in trace
        self.trace_recorder.record(
            "fault.detected.requirement_change",
            {
                "fault_id": fault.fault_id,
                "node_id": node_id,
                "constraint_count": len(new_constraints),
            }
        )
        
        return fault
    
    def detect_node_failure(self, node_id: str, error_message: str) -> Fault:
        """Detect a node failure fault.
        
        Args:
            node_id: The node that failed
            error_message: The error message
            
        Returns:
            Detected fault
        """
        fault = Fault(
            fault_id=f"fault:node_failure:{node_id}:{len(self.faults)}",
            fault_type="node_failure",
            target=node_id,
            payload={"error_message": error_message},
            timestamp="",  # This would be set to current timestamp
            metadata={"detector": "FaultDetector"}
        )
        
        self.faults.append(fault)
        
        # Record in trace
        self.trace_recorder.record(
            "fault.detected.node_failure",
            {
                "fault_id": fault.fault_id,
                "node_id": node_id,
                "error": error_message,
            }
        )
        
        return fault
    
    def detect_tool_failure(self, tool_name: str, error_message: str) -> Fault:
        """Detect a tool failure fault.
        
        Args:
            tool_name: The tool that failed
            error_message: The error message
            
        Returns:
            Detected fault
        """
        fault = Fault(
            fault_id=f"fault:tool_failure:{tool_name}:{len(self.faults)}",
            fault_type="tool_failure",
            target=tool_name,
            payload={"error_message": error_message},
            timestamp="",  # This would be set to current timestamp
            metadata={"detector": "FaultDetector"}
        )
        
        self.faults.append(fault)
        
        # Record in trace
        self.trace_recorder.record(
            "fault.detected.tool_failure",
            {
                "fault_id": fault.fault_id,
                "tool_name": tool_name,
                "error": error_message,
            }
        )
        
        return fault
    
    def detect_memory_loss(self, lost_facts: List[str]) -> Fault:
        """Detect a memory loss fault.
        
        Args:
            lost_facts: List of fact IDs that were lost
            
        Returns:
            Detected fault
        """
        fault = Fault(
            fault_id=f"fault:memory_loss:{len(self.faults)}",
            fault_type="memory_loss",
            target="memory_system",
            payload={"lost_facts": lost_facts},
            timestamp="",  # This would be set to current timestamp
            metadata={"detector": "FaultDetector"}
        )
        
        self.faults.append(fault)
        
        # Record in trace
        self.trace_recorder.record(
            "fault.detected.memory_loss",
            {
                "fault_id": fault.fault_id,
                "lost_fact_count": len(lost_facts),
            }
        )
        
        return fault
    
    def detect_resource_degradation(self, resource_id: str, degradation_type: str) -> Fault:
        """Detect a resource degradation fault.
        
        Args:
            resource_id: The resource that degraded
            degradation_type: Type of degradation
            
        Returns:
            Detected fault
        """
        fault = Fault(
            fault_id=f"fault:resource_degradation:{resource_id}:{len(self.faults)}",
            fault_type="resource_degradation",
            target=resource_id,
            payload={"degradation_type": degradation_type},
            timestamp="",  # This would be set to current timestamp
            metadata={"detector": "FaultDetector"}
        )
        
        self.faults.append(fault)
        
        # Record in trace
        self.trace_recorder.record(
            "fault.detected.resource_degradation",
            {
                "fault_id": fault.fault_id,
                "resource_id": resource_id,
                "degradation_type": degradation_type,
            }
        )
        
        return fault
    
    def detect_fault(self, fault_type: str, target: str, fault_data: dict) -> Fault:
        """Dispatch a typed fault to the canonical detector.
        
        This method is the single public detection entry point. Recovery policy is
        deliberately absent here and belongs to GlobalGovernor.
        
        Args:
            fault_type: Type of fault (requirement_change, node_failure, tool_failure, 
                       memory_loss, resource_degradation, or any other type)
            target: Target node or component ID
            fault_data: Dictionary containing fault-specific data
            
        Returns:
            Detected fault object
        """
        from datetime import datetime, timezone
        
        if fault_type == "requirement_change":
            new_constraints = fault_data.get("new_constraints", fault_data.get("change", ""))
            if isinstance(new_constraints, str):
                new_constraints = [new_constraints]
            # Create fault with full payload including affected_nodes
            fault = Fault(
                fault_id=f"fault:req_change:{target}:{len(self.faults)}",
                fault_type="requirement_change",
                target=target,
                payload=fault_data,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"detector": "FaultDetector"}
            )
            self.faults.append(fault)
            self.trace_recorder.record(
                "fault.detected.requirement_change",
                {"fault_id": fault.fault_id, "target": target}
            )
            return fault
        elif fault_type == "node_failure":
            error_message = fault_data.get("error_message", "Unknown error")
            return self.detect_node_failure(target, error_message)
        elif fault_type == "tool_failure":
            error_message = fault_data.get("error_message", "Unknown error")
            return self.detect_tool_failure(target, error_message)
        elif fault_type in ("memory_loss", "memory_missing"):
            lost_facts = fault_data.get("lost_facts", [fault_data.get("missing", "")])
            return self.detect_memory_loss(lost_facts)
        elif fault_type == "resource_degradation":
            degradation_type = fault_data.get("degradation_type", "unknown")
            return self.detect_resource_degradation(target, degradation_type)
        else:
            # Generic fault for unknown types
            fault = Fault(
                fault_id=f"fault:{fault_type}:{target}:{len(self.faults)}",
                fault_type=fault_type,
                target=target,
                payload=fault_data,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"detector": "FaultDetector"}
            )
            self.faults.append(fault)
            self.trace_recorder.record(
                "fault.detected.generic",
                {
                    "fault_id": fault.fault_id,
                    "fault_type": fault_type,
                    "target": target
                }
            )
            return fault


class ImpactAnalyzer:
    """Analyzes the impact of faults on the task graph."""
    
    def __init__(self, task_graph: TaskGraph):
        self.task_graph = task_graph
    
    def analyze_impact(self, fault: Fault) -> Dict[str, Any]:
        """Analyze the impact of a fault.
        
        Args:
            fault: The fault to analyze
            
        Returns:
            Dictionary with impact analysis results
        """
        impacted_nodes = set()
        impacted_edges = set()
        reusable_artifacts = set()
        invalidated_facts = set()
        
        if fault.fault_type == "requirement_change":
            # For requirement changes, use affected_nodes from payload if provided
            affected_nodes = fault.payload.get("affected_nodes", [])
            if affected_nodes:
                # Use explicitly provided affected nodes
                impacted_nodes = set(affected_nodes)
            else:
                # Fallback to finding descendants
                target_node_id = fault.target
                impacted_nodes = self._find_descendants(target_node_id)
                impacted_nodes.add(target_node_id)
            
            # Find related edges
            for edge in self.task_graph.edges:
                if edge.source in impacted_nodes or edge.target in impacted_nodes:
                    impacted_edges.add((edge.source, edge.target))
            
            # Some artifacts might still be reusable
            reusable_artifacts = self._find_reusable_artifacts(impacted_nodes)
            
            # Facts related to the changed constraints are invalidated
            new_constraints = fault.payload.get("new_constraints", [])
            invalidated_facts = self._find_invalidated_facts(new_constraints)
            
        elif fault.fault_type == "node_failure":
            # For node failures, impact the node and its dependents
            target_node_id = fault.target
            impacted_nodes = self._find_dependents(target_node_id)
            impacted_nodes.add(target_node_id)
            
            # Find related edges
            for edge in self.task_graph.edges:
                if edge.source in impacted_nodes or edge.target in impacted_nodes:
                    impacted_edges.add((edge.source, edge.target))
            
            # Some artifacts might still be reusable
            reusable_artifacts = self._find_reusable_artifacts(impacted_nodes)
            
        elif fault.fault_type == "tool_failure":
            # For tool failures, impact nodes that use the tool
            tool_name = fault.target
            impacted_nodes = self._find_nodes_using_tool(tool_name)
            
            # Find related edges
            for edge in self.task_graph.edges:
                if edge.source in impacted_nodes or edge.target in impacted_nodes:
                    impacted_edges.add((edge.source, edge.target))
            
            # Some artifacts might still be reusable
            reusable_artifacts = self._find_reusable_artifacts(impacted_nodes)
            
        elif fault.fault_type == "memory_loss":
            # For memory loss, identify nodes that depend on lost facts
            lost_facts = fault.payload.get("lost_facts", [])
            impacted_nodes = self._find_nodes_dependent_on_facts(lost_facts)
            
            # Find related edges
            for edge in self.task_graph.edges:
                if edge.source in impacted_nodes or edge.target in impacted_nodes:
                    impacted_edges.add((edge.source, edge.target))
            
            # Lost facts are invalidated
            invalidated_facts = set(lost_facts)
            
        elif fault.fault_type == "resource_degradation":
            # For resource degradation, impact nodes assigned to the resource
            resource_id = fault.target
            impacted_nodes = self._find_nodes_using_resource(resource_id)
            
            # Find related edges
            for edge in self.task_graph.edges:
                if edge.source in impacted_nodes or edge.target in impacted_nodes:
                    impacted_edges.add((edge.source, edge.target))
            
            # Some artifacts might still be reusable
            reusable_artifacts = self._find_reusable_artifacts(impacted_nodes)
        
        return {
            "impacted_nodes": sorted(list(impacted_nodes)),
            "impacted_edges": sorted(list(impacted_edges)),
            "reusable_artifacts": sorted(list(reusable_artifacts)),
            "invalidated_facts": sorted(list(invalidated_facts)),
            "impact_scope": len(impacted_nodes) / len(self.task_graph.nodes) if self.task_graph.nodes else 0
        }
    
    def _find_descendants(self, node_id: str) -> Set[str]:
        """Find all descendant nodes of a given node.
        
        Args:
            node_id: The node ID to find descendants for
            
        Returns:
            Set of descendant node IDs
        """
        descendants = set()
        visited = set()
        queue = [node_id]
        
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            
            # Find direct children
            for edge in self.task_graph.edges:
                if edge.source == current and edge.edge_type == "depends_on":
                    child = edge.target
                    if child not in visited:
                        descendants.add(child)
                        queue.append(child)
        
        return descendants
    
    def _find_dependents(self, node_id: str) -> Set[str]:
        """Find all nodes that depend on a given node (transitive).

        Args:
            node_id: The node ID to find dependents for

        Returns:
            Set of dependent node IDs (including transitive dependents)
        """
        dependents = set()
        visited = set()
        queue = [node_id]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            # Find nodes that depend on current node
            for edge in self.task_graph.edges:
                if edge.target == current and edge.edge_type == "depends_on":
                    dependent = edge.source
                    if dependent not in visited:
                        dependents.add(dependent)
                        queue.append(dependent)

        return dependents
    
    def _find_reusable_artifacts(self, impacted_nodes: Set[str]) -> Set[str]:
        """Find artifacts that might still be reusable despite the impact.
        
        Analyzes artifact metadata to determine which artifacts remain valid
        even when their source nodes are impacted. Artifacts from nodes that
        are NOT in the impacted set are considered reusable.
        
        Args:
            impacted_nodes: Set of impacted node IDs
            
        Returns:
            Set of reusable artifact IDs
        """
        reusable = set()
        for artifact_id, artifact in self.task_graph.artifacts.items():
            # Artifact is reusable if its source node is NOT impacted
            source_node = getattr(artifact, 'node_id', None) or artifact.metadata.get('node_id', '')
            if source_node and source_node not in impacted_nodes:
                reusable.add(artifact_id)
            elif not source_node:
                # Artifacts without a clear source node are conservatively reusable
                reusable.add(artifact_id)
        return reusable
    
    def _find_invalidated_facts(self, new_constraints: List[str]) -> Set[str]:
        """Find facts that are invalidated by new constraints.
        
        Checks each fact's text against new constraint keywords.
        A fact is invalidated if any new constraint keyword appears in the fact text,
        suggesting the constraint change may contradict the fact.
        
        Args:
            new_constraints: List of new constraints
            
        Returns:
            Set of invalidated fact IDs
        """
        invalidated: set[str] = set()
        if not new_constraints:
            return invalidated
        
        # Extract keywords from new constraints
        constraint_keywords: set[str] = set()
        for constraint in new_constraints:
            words = constraint.lower().split()
            constraint_keywords.update(w for w in words if len(w) > 3)
        
        for fact_id, fact in self.task_graph.facts.items():
            fact_text_lower = fact.text.lower()
            # Check if any constraint keyword appears in the fact
            # and the fact has low confidence (more likely to be invalidated)
            keyword_overlap = sum(1 for kw in constraint_keywords if kw in fact_text_lower)
            if keyword_overlap >= 2 and fact.confidence < 0.7:
                invalidated.add(fact_id)
        
        return invalidated
    
    def _find_nodes_using_tool(self, tool_name: str) -> Set[str]:
        """Find nodes that use a specific tool.
        
        Checks node metadata for tool usage records and tool assignments.
        
        Args:
            tool_name: The tool name
            
        Returns:
            Set of node IDs
        """
        using_nodes = set()
        for node_id, node in self.task_graph.nodes.items():
            # Check metadata for tool references
            metadata = node.metadata if hasattr(node, 'metadata') else {}
            tools_used = metadata.get('tools_used', [])
            if tool_name in tools_used:
                using_nodes.add(node_id)
            role = node.role.lower() if hasattr(node, 'role') else ''
            # Use the same canonical role toolset as Worker execution.  A
            # second hard-coded map here previously referenced terminal and
            # code_exec, so recovery analysis could report a tool dependency
            # that could never exist at runtime.
            try:
                from AstralNexus.tools.toolsets import resolve_toolset
                role_tools = set(resolve_toolset(role))
            except Exception:
                role_tools = set()
            if tool_name in role_tools:
                using_nodes.add(node_id)
        return using_nodes
    
    def _find_nodes_dependent_on_facts(self, fact_ids: List[str]) -> Set[str]:
        """Find nodes that depend on specific facts.
        
        Checks if any fact's source node has downstream dependents.
        Nodes whose goals reference fact content are also included.
        
        Args:
            fact_ids: List of fact IDs
            
        Returns:
            Set of node IDs
        """
        dependent_nodes = set()
        fact_id_set = set(fact_ids)
        
        for fact_id in fact_ids:
            fact = self.task_graph.facts.get(fact_id)
            if not fact:
                continue
            
            # Source node's dependents are affected
            source = fact.source if hasattr(fact, 'source') else ''
            if source:
                dependents = self._find_descendants(source)
                dependent_nodes.update(dependents)
            
            # Also find nodes whose goals reference the fact text
            fact_keywords = set(fact.text.lower().split()[:5])
            for node_id, node in self.task_graph.nodes.items():
                goal_words = set(node.goal.lower().split())
                if fact_keywords & goal_words and normalize_status(node.status) in {
                    "admitted", "waiting_dependencies", "ready", "running", "repair_pending"
                }:
                    dependent_nodes.add(node_id)
        
        return dependent_nodes
    
    def _find_nodes_using_resource(self, resource_id: str) -> Set[str]:
        """Find nodes that use a specific resource.
        
        Checks node metadata for resource assignments and resource tier mappings.
        
        Args:
            resource_id: The resource ID
            
        Returns:
            Set of node IDs
        """
        using_nodes = set()
        for node_id, node in self.task_graph.nodes.items():
            metadata = node.metadata if hasattr(node, 'metadata') else {}
            # Check explicit resource assignment
            assigned = metadata.get('assigned_resource', '')
            if assigned == resource_id:
                using_nodes.add(node_id)
            # Check resource tier
            resource_tier = getattr(node, 'resource_tier', '')
            if resource_tier and resource_id in str(resource_tier):
                using_nodes.add(node_id)
        return using_nodes


class RecoveryExecutor:
    """Execute Governor-approved recovery actions."""
    
    def __init__(self, task_graph: TaskGraph, trace_recorder: TraceRecorder):
        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self._analyzer = ImpactAnalyzer(task_graph)
    
    def _find_descendants(self, node_id: str) -> Set[str]:
        """Delegate to ImpactAnalyzer."""
        return self._analyzer._find_descendants(node_id)
    
    def _find_nodes_using_tool(self, tool_name: str) -> Set[str]:
        """Delegate to ImpactAnalyzer."""
        return self._analyzer._find_nodes_using_tool(tool_name)
    
    def _find_nodes_using_resource(self, resource_id: str) -> Set[str]:
        """Delegate to ImpactAnalyzer."""
        return self._analyzer._find_nodes_using_resource(resource_id)
    
    def execute_recovery(self, fault: Fault, recovery_plan: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute a complete recovery plan.
        
        Args:
            fault: The fault being recovered
            recovery_plan: List of recovery actions to execute
            
        Returns:
            Dictionary with execution results
        """
        results = []
        success_count = 0
        failure_count = 0
        
        for action in recovery_plan:
            try:
                success = self.execute_recovery_action(action)
                results.append({
                    "action": action,
                    "success": success
                })
                if success:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                results.append({
                    "action": action,
                    "success": False,
                    "error": str(e)
                })
                failure_count += 1
        
        # Record execution results
        self.trace_recorder.record(
            "recovery.executed",
            {
                "fault_id": fault.fault_id,
                "action_count": len(recovery_plan),
                "success_count": success_count,
                "failure_count": failure_count
            }
        )
        
        return {
            "total_actions": len(recovery_plan),
            "success_count": success_count,
            "failure_count": failure_count,
            "results": results
        }
    
    def execute_recovery_action(self, action: Dict[str, Any]) -> bool:
        """Execute a recovery action.
        
        Args:
            action: The recovery action to execute
            
        Returns:
            True if successful, False otherwise
        """
        action_type = action.get("type")
        target = action.get("target")
        
        try:
            if action_type == "restore_facts":
                return self._restore_facts(action.get("details", {}))
            if not isinstance(target, str) or not target:
                raise ValueError("Recovery action requires a non-empty target node ID")
            if action_type == "replan":
                return self._replan_node(target, action.get("details", {}))
            elif action_type == "retry":
                return self._retry_node(target)
            elif action_type == "reassign":
                return self._reassign_node(target, action.get("details", {}))
            elif action_type == "switch_tool":
                return self._switch_tool(target, action.get("details", {}))
            elif action_type == "restore_facts":
                return self._restore_facts(action.get("details", {}))
            elif action_type == "switch_resource":
                return self._switch_resource(target, action.get("details", {}))
            else:
                # Unknown action type
                self.trace_recorder.record(
                    "recovery.action.unknown",
                    {
                        "action_type": action_type,
                        "target": target
                    }
                )
                return False
        except Exception as e:
            # Handle execution errors
            self.trace_recorder.record(
                "recovery.action.error",
                {
                    "action_type": action_type,
                    "target": target,
                    "error": str(e)
                }
            )
            return False
    
    def _request_node_repair(self, node_id: str) -> bool:
        """Move a node into the repair lifecycle using legal transitions.

        Recovery must not write ad-hoc states such as ``pending`` directly.
        A running node first records a failed execution, while admitted or
        dependency-waiting nodes remain schedulable in their current state.
        """
        node = self.task_graph.get_node(node_id)
        if not node:
            return False
        status = normalize_status(node.status)
        if status == "repair_pending":
            return True
        if status == "running":
            self.task_graph.update_node_status(node_id, "failed")
            status = "failed"
        if status in {"result_submitted", "verifying", "failed"}:
            self.task_graph.update_node_status(node_id, "repair_pending")
            return True
        if status in {"admitted", "waiting_dependencies", "ready", "blocked"}:
            self.task_graph.update_node_status(node_id, "repair_pending")
            return True
        return False

    def _replan_node(self, node_id: str, details: Dict[str, Any]) -> bool:
        """Replan a node due to requirement changes.
        
        Args:
            node_id: The node ID to replan
            details: Additional details
            
        Returns:
            True if successful, False otherwise
        """
        node = self.task_graph.get_node(node_id)
        if not node:
            return False
        
        # Step 1: Mark descendants for replanning
        descendants = self._find_descendants(node_id)
        for desc_id in descendants:
            desc_node = self.task_graph.get_node(desc_id)
            if desc_node and normalize_status(desc_node.status) in {
                "admitted", "waiting_dependencies", "ready", "running", "blocked",
                "result_submitted", "verifying", "failed"
            }:
                self._request_node_repair(desc_id)
                desc_node.metadata["replan_cascade"] = True
        
        # Step 2: Reset the node itself
        if normalize_status(node.status) not in {"completed", "cancelled", "superseded"}:
            self._request_node_repair(node_id)
        node.metadata["replanned"] = True
        node.metadata["replan_reason"] = details
        node.retry_count = 0  # Reset retry count for replanned task
        
        # Step 3: Update constraints if new constraints provided
        new_constraints = details.get("new_constraints", [])
        if new_constraints:
            if "constraints" not in node.metadata:
                node.metadata["constraints"] = []
            node.metadata["constraints"].extend(new_constraints)
        
        self.trace_recorder.record(
            "recovery.action.replan",
            {
                "node_id": node_id,
                "reason": details.get("new_constraints", "Requirement change"),
                "descendants_affected": len(descendants),
                "new_constraints": new_constraints
            }
        )
        
        return True
    
    def _retry_node(self, node_id: str) -> bool:
        """Retry a failed node.
        
        Args:
            node_id: The node ID to retry
            
        Returns:
            True if successful, False otherwise
        """
        node = self.task_graph.get_node(node_id)
        if node:
            # Fix P5#15 (r17 crash): never rearm a leased, actively executing
            # node — the running->ready clobber desyncs graph state from the
            # live worker and the later ready->failed bookkeeping crashed the
            # whole run.  Skip with an honest event; the recovery loop can
            # target something else.
            if str(node.status) == "running":
                self.trace_recorder.record("recovery.action.skip_running_node", {
                    "node_id": node_id,
                    "action": "retry",
                    "reason": "node is leased and executing; rearm would clobber running state",
                })
                return False
            if not self._request_node_repair(node_id):
                return False
            self.task_graph.update_node_status(node_id, "ready")
            node.retry_count += 1
            
            self.trace_recorder.record(
                "recovery.action.retry",
                {
                    "node_id": node_id,
                    "retry_count": node.retry_count
                }
            )
            
            return True
        
        return False
    
    def _reassign_node(self, node_id: str, details: Dict[str, Any]) -> bool:
        """Apply a Governor-approved Worker or resource reassignment.
        
        Args:
            node_id: The node ID to reassign
            
        Returns:
            True if successful, False otherwise
        """
        node = self.task_graph.get_node(node_id)
        if not node:
            return False
        # Fix P5#15 (r17 crash): same leased-node clobber guard as _retry_node.
        if str(node.status) == "running":
            self.trace_recorder.record("recovery.action.skip_running_node", {
                "node_id": node_id,
                "action": "reassign",
                "reason": "node is leased and executing; rearm would clobber running state",
            })
            return False
        
        # Step 1: Determine current assignment
        current_resource = node.metadata.get("assigned_resource", "default")
        current_agent = node.metadata.get("assigned_agent", node.role)
        
        next_tier = str(details.get("resource_tier") or "").strip()
        next_worker = str(details.get("worker_type") or "").strip()
        if not next_tier and not next_worker:
            raise ValueError(
                "reassign action requires Governor-approved resource_tier or worker_type"
            )
        if (node.metadata.get("reassigned")
                and (not next_worker or next_worker == node.metadata.get("approved_worker_type"))
                and (not next_tier or next_tier == node.resource_tier)):
            self.trace_recorder.record("recovery.action.reassign_no_change", {
                "node_id": node_id, "worker_type": next_worker,
                "resource_tier": next_tier, "action_required": "replan_or_change_assignment",
            })
            return False
        
        # Step 3: Update node assignment
        self._request_node_repair(node_id)
        # Reassignment is a completed recovery decision, not an open-ended
        # repair request.  Move the node back into the scheduler's ready set
        # after changing its resource/worker metadata; otherwise the graph
        # remains in repair_pending forever and the orchestrator stalls.
        self.task_graph.update_node_status(node_id, "ready")
        node.metadata["reassigned"] = True
        node.metadata["previous_resource"] = current_resource
        node.metadata["previous_agent"] = current_agent
        if next_tier:
            node.metadata["resource_tier"] = next_tier
            node.resource_tier = next_tier
        if next_worker:
            node.metadata["assigned_agent"] = next_worker
            node.metadata["approved_worker_type"] = next_worker
        node.metadata["reassignment_count"] = node.metadata.get("reassignment_count", 0) + 1
        
        # Step 4: Reset retry count for reassigned task
        node.retry_count = 0
        
        self.trace_recorder.record(
            "recovery.action.reassign",
            {
                "node_id": node_id,
                "from_resource": current_resource,
                "to_tier": next_tier or node.resource_tier,
                "to_worker": next_worker or current_agent,
                "reassignment_count": node.metadata["reassignment_count"]
            }
        )
        
        return True
    
    def _switch_tool(self, tool_name: str, details: Dict[str, Any]) -> bool:
        """Switch to an alternative tool.
        
        Args:
            tool_name: The tool name to switch from
            details: Additional details
            
        Returns:
            True if successful, False otherwise
        """
        alternative_tool = str(details.get("alternative_tool") or "").strip()
        if not alternative_tool:
            raise ValueError(
                "switch_tool action requires Governor-approved alternative_tool"
            )
        
        # Update task assignments to use the alternative tool
        affected_nodes = self._find_nodes_using_tool(tool_name)
        for affected_node_id in affected_nodes:
            node = self.task_graph.get_node(affected_node_id)
            if node and normalize_status(node.status) in {
                "admitted", "waiting_dependencies", "ready", "running", "repair_pending"
            }:
                node.metadata["tool_switch"] = {
                    "from": tool_name,
                    "to": alternative_tool,
                    "reason": details.get("error_message", "Tool failure"),
                }
        
        self.trace_recorder.record(
            "recovery.action.switch_tool",
            {
                "old_tool": tool_name,
                "new_tool": alternative_tool,
                "affected_nodes": list(affected_nodes),
                "error": details.get("error_message", "Tool failure")
            }
        )
        
        return True
    
    def _restore_facts(self, details: Dict[str, Any]) -> bool:
        """Restore lost facts.
        
        Args:
            details: Details about lost facts
            
        Returns:
            True if successful, False otherwise
        """
        lost_facts = details.get("lost_facts", [])
        
        # Attempt to recover facts from task graph and memory federation
        recovered_count = 0
        for fact_id in lost_facts:
            # Check if fact exists in task graph
            if fact_id in self.task_graph.facts:
                recovered_count += 1
                continue
            
            # Try to re-derive from artifacts
            for artifact_id, artifact in self.task_graph.artifacts.items():
                if fact_id in artifact.description or fact_id in artifact_id:
                    # Re-create fact from artifact
                    from datetime import datetime, timezone
                    recovered_fact = {
                        "fact_id": fact_id,
                        "text": f"Recovered from {artifact_id}: {artifact.description}",
                        "source": artifact.node_id,
                        "confidence": 0.7,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self.task_graph.add_fact(recovered_fact)
                    recovered_count += 1
                    break
        
        self.trace_recorder.record(
            "recovery.action.restore_facts",
            {
                "fact_count": len(lost_facts),
                "recovered_count": recovered_count,
                "lost_facts": lost_facts
            }
        )
        
        return recovered_count > 0
    
    def _switch_resource(self, resource_id: str, details: Dict[str, Any]) -> bool:
        """Switch to an alternative resource.
        
        Args:
            resource_id: The resource ID to switch from
            details: Additional details
            
        Returns:
            True if successful, False otherwise
        """
        alternative_resource = str(
            details.get("alternative_resource") or ""
        ).strip()
        if not alternative_resource:
            raise ValueError(
                "switch_resource action requires Governor-approved alternative_resource"
            )
        degradation_type = details.get("degradation_type", "Unknown")
        
        # Update task assignments to use the alternative resource
        affected_nodes = self._find_nodes_using_resource(resource_id)
        for affected_node_id in affected_nodes:
            node = self.task_graph.get_node(affected_node_id)
            if node and normalize_status(node.status) in {
                "admitted", "waiting_dependencies", "ready", "running", "repair_pending"
            }:
                node.metadata["resource_switch"] = {
                    "from": resource_id,
                    "to": alternative_resource,
                    "reason": degradation_type,
                }
        
        # Mark the degraded resource as unavailable
        degraded_resource = self.task_graph.get_node(resource_id) if resource_id.startswith("T-") else None
        if degraded_resource:
            if normalize_status(degraded_resource.status) in {"ready", "running"}:
                self.task_graph.update_node_status(resource_id, "blocked")
            degraded_resource.metadata["degraded"] = True
            degraded_resource.metadata["degradation_type"] = degradation_type
        
        self.trace_recorder.record(
            "recovery.action.switch_resource",
            {
                "old_resource": resource_id,
                "new_resource": alternative_resource,
                "affected_nodes": list(affected_nodes),
                "degradation_type": degradation_type
            }
        )
        
        return True



class FaultRecoveryEngine:
    """Execute recovery diagnostics and actions on the active task graph.

    GlobalGovernor owns recovery policy. This engine never discovers a graph
    from storage and never creates an alternate trace sink.
    """
    
    def __init__(self, task_graph: TaskGraph, trace_recorder: TraceRecorder):
        if not isinstance(task_graph, TaskGraph):
            raise TypeError(f"Expected TaskGraph, got {type(task_graph).__name__}")
        if trace_recorder is None or not callable(getattr(trace_recorder, "record", None)):
            raise TypeError("trace_recorder with record() is required")
        self.task_graph = task_graph
        self.trace_recorder = trace_recorder
        self.detector = FaultDetector(self.task_graph, self.trace_recorder)
        self.analyzer = ImpactAnalyzer(self.task_graph)
        self.executor = RecoveryExecutor(self.task_graph, self.trace_recorder)
        self.evaluator = EvaluationReporter(self.task_graph, self.trace_recorder)
    
    def execute_governed_recovery(
        self,
        fault: Fault,
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Execute actions already selected by GlobalGovernor."""
        result = self.executor.execute_recovery(fault, actions)
        fault.status = (
            FaultStatus.RECOVERED
            if result["failure_count"] == 0
            else FaultStatus.FAILED
        )
        return result
    
    def get_recovery_report(self) -> Dict[str, Any]:
        """Generate a comprehensive recovery report.
        
        Returns:
            Dictionary containing recovery metrics and analysis
        """
        return self.evaluator.generate_recovery_report()
