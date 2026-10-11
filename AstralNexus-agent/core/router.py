from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from AstralNexus.core.agent_registry import AgentRole, get_agent_registry
from AstralNexus.core.task_graph import TaskGraph, TaskNode


@dataclass
class ReadyWaveMetrics:
    """Metrics about ready-wave parallelism.
    
    Tracks the width and characteristics of each ready-wave to prove
    that parallelism comes from real dependency satisfaction, not fixed batching.
    """
    wave_id: int
    wave_width: int  # Number of nodes in this wave
    node_ids: List[str]
    roles: List[str]
    timestamp: float = 0.0


class ReadyWaveTracker:
    """Tracks ready-wave metrics across execution.
    
    Provides evidence that parallelism comes from real dependency satisfaction
    (dynamic ready-wave) rather than fixed batching.
    """
    
    def __init__(self):
        self.waves: List[ReadyWaveMetrics] = []
        self.wave_counter = 0
    
    def record_wave(self, nodes: List[TaskNode]) -> Optional[ReadyWaveMetrics]:
        """Record a ready-wave.
        
        Args:
            nodes: The ready nodes in this wave
            
        Returns:
            ReadyWaveMetrics if wave was recorded, None if empty
        """
        if not nodes:
            return None
        
        self.wave_counter += 1
        import time
        wave = ReadyWaveMetrics(
            wave_id=self.wave_counter,
            wave_width=len(nodes),
            node_ids=[n.node_id for n in nodes],
            roles=[n.role for n in nodes],
            timestamp=time.time(),
        )
        self.waves.append(wave)
        return wave
    
    def get_stats(self) -> Dict[str, Any]:
        """Get ready-wave statistics.
        
        Returns:
            Dictionary with avg_width, max_width, min_width, total_waves,
            width_distribution, and role_diversity.
        """
        if not self.waves:
            return {
                "total_waves": 0,
                "avg_width": 0.0,
                "max_width": 0,
                "min_width": 0,
                "width_distribution": {},
                "role_diversity": {},
            }
        
        widths = [w.wave_width for w in self.waves]
        
        # Width distribution
        width_dist: Dict[int, int] = {}
        for w in widths:
            width_dist[w] = width_dist.get(w, 0) + 1
        
        # Role diversity per wave
        role_diversity: Dict[int, float] = {}
        for wave in self.waves:
            unique_roles = len(set(wave.roles))
            role_diversity[wave.wave_id] = unique_roles / max(1, wave.wave_width)
        
        return {
            "total_waves": len(self.waves),
            "avg_width": sum(widths) / len(widths),
            "max_width": max(widths),
            "min_width": min(widths),
            "median_width": sorted(widths)[len(widths) // 2],
            "width_distribution": width_dist,
            "avg_role_diversity": (
                sum(role_diversity.values()) / len(role_diversity)
                if role_diversity else 0.0
            ),
            "waves_with_width_ge_4": sum(1 for w in widths if w >= 4),
            "waves_with_width_ge_2": sum(1 for w in widths if w >= 2),
        }


class Router:
    """Router for dynamically selecting agents and resources for task execution."""
    
    def __init__(
        self,
        task_graph: TaskGraph,
        trace_recorder=None,
        max_parallel: int | None = None,
    ):
        """Initialize router.
        
        Args:
            task_graph: Canonical in-memory semantic task graph.
            trace_recorder: Optional trace recorder
            max_parallel: Maximum number of nodes admitted to one ready wave.
        """
        if not isinstance(task_graph, TaskGraph):
            raise TypeError("Router requires the canonical TaskGraph instance")
        self.task_graph = task_graph
        
        self.agent_registry = get_agent_registry()
        self.trace_recorder = trace_recorder
        self.max_parallel = max_parallel or 4
        
        # Ready-wave tracking for parallelism evidence (阶段五 P0)
        self.wave_tracker = ReadyWaveTracker()
        
        # 阶段六: ValueRouter for low-entropy communication filtering
        from AstralNexus.core.value_router import ValueRouter
        self.value_router = ValueRouter(
            self.task_graph,
            value_threshold=0.3,
        )
    
    # ------------------------------------------------------------------
    # Public API methods (required by tests)
    # ------------------------------------------------------------------
    
    def select_ready_nodes(self) -> List[TaskNode]:
        """Select nodes that are ready for execution.
        
        Records ready-wave metrics for parallelism evidence.
        
        Returns:
            List of TaskNode objects that are ready to be executed
        """
        ready = self.task_graph.get_ready_nodes()

        # Ready-gate (r13c live, 2026-09-29 08:07:08Z, commit_seq 161-164):
        # status=="ready" is a claim, not a proof.  A verification-recovery
        # patch added producer N-120, the dependency edge N-118->N-120, and
        # the repair_pending->ready requeue of N-118 in ONE commit; trusting
        # the flag alone let the wave start N-118 before N-120 ever ran --
        # the consumer executed ahead of the producer its own root-cause
        # routing had just created.  Scheduling must therefore apply the
        # SAME dependency/evidence gate as promotion (readiness_blockers is
        # documented as "the same gate as scheduling"): a ready node with
        # unsatisfied blockers is deferred -- never executed, never
        # re-statused -- until its producers finish.  Repair leniency for
        # failed/repair_pending parents lives inside dependency_available
        # and is preserved unchanged.
        selectable: List[TaskNode] = []
        deferred: List[Dict[str, Any]] = []
        for node in ready:
            blockers = self.task_graph.readiness_blockers(node)
            if any(blockers.values()):
                deferred.append({"node_id": node.node_id, **blockers})
            else:
                selectable.append(node)
        if deferred and self.trace_recorder:
            self.trace_recorder.record("router.ready_gate.deferred", {
                "deferred_count": len(deferred),
                "deferred": deferred,
            })
        ready = selectable

        # Record ready-wave for parallelism evidence (阶段五 P0)
        if ready:
            wave = self.wave_tracker.record_wave(ready)
            if wave and self.trace_recorder:
                self.trace_recorder.record("router.ready_wave", {
                    "wave_id": wave.wave_id,
                    "wave_width": wave.wave_width,
                    "node_ids": wave.node_ids,
                    "roles": wave.roles,
                })
        
        return ready
    
    def get_wave_stats(self) -> Dict[str, Any]:
        """Get ready-wave statistics for evaluation reporting.
        
        Returns:
            Dictionary with wave metrics (avg_width, max_width, etc.)
        """
        return self.wave_tracker.get_stats()
    
    def encode_node_features(self, node: TaskNode) -> Dict:
        """Encode node features for routing decisions.
        
        Args:
            node: The task node to encode
            
        Returns:
            Dictionary containing encoded features
        """
        dep_depth = self._calculate_dependency_depth(node)
        blocked_count = self._get_blocked_upstream_count(node)
        risk_score = self._calculate_risk_score(node)
        
        # Create a simple goal embedding using keyword extraction
        keywords = list(self._extract_keywords(node.goal))
        goal_embedding = [hash(kw) % 100 for kw in keywords] if keywords else [0]
        
        return {
            "node_id": node.node_id,
            "goal": node.goal,
            "goal_embedding": goal_embedding,
            "role": node.role,
            "dependency_count": len(node.dependencies),
            "dependency_depth": dep_depth,
            "blocked_upstream": blocked_count,
            "risk_level": node.risk_level,
            "sensitivity": node.sensitivity,
            "resource_tier": node.resource_tier,
            "risk_score": risk_score,
            "retry_count": node.retry_count,
            "keywords": keywords,
        }
    
    def choose_role(self, node: TaskNode) -> str:
        """Choose the most appropriate role for a task node.
        
        Args:
            node: The task node
            
        Returns:
            The name of the chosen role
        """
        return self._select_agent_for_task(node)

    def match_worker_profile(self, node: TaskNode, profile: Dict) -> str:
        """Bind a Governor-approved capability profile to a real role factory."""
        if not isinstance(profile, dict) or not profile:
            raise LookupError(
                f"Router cannot match task {node.node_id} without a worker profile"
            )
        worker_type = str(profile.get("worker_type") or "").strip()
        if not worker_type:
            raise LookupError(
                f"Router cannot match task {node.node_id}: worker_type is missing"
            )

        canonical = self.agent_registry.resolve_role(worker_type)
        if self.agent_registry.get_role_factory(canonical) is not None:
            return canonical

        required = {
            str(item).strip().lower()
            for item in profile.get("capabilities", [])
            if str(item).strip()
        }
        candidates = []
        for role_name in self.agent_registry.list_roles():
            if self.agent_registry.get_role_factory(role_name) is None:
                continue
            role = self.agent_registry.get_role(role_name)
            supported = {
                str(item).strip().lower()
                for item in (role.capabilities if role else [])
                if str(item).strip()
            }
            overlap = len(required & supported)
            if overlap:
                candidates.append((overlap / max(1, len(required)), role_name))

        if not candidates:
            raise LookupError(
                f"Router found no executable role for Governor profile "
                f"{worker_type!r} on task {node.node_id}"
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1]
    
    def retrieve_minimal_context(self, node: TaskNode) -> Dict:
        """Retrieve minimal context needed for task execution.
        
        Args:
            node: The task node
            
        Returns:
            Dictionary containing minimal context
        """
        context: Dict[str, Any] = {
            "constraints": node.constraints,
            "recent_decisions": [],
            "relevant_facts": [],
            "relevant_artifacts": [],
            "recent_failures": [],
        }
        # A retry must see the concrete failure that caused re-admission.
        # Without this, the worker receives only a generic execution_error
        # and may hallucinate graph corruption or duplicate dependencies.
        failure = node.metadata.get("last_failure") or node.metadata.get("error")
        if failure:
            if isinstance(failure, dict):
                context["recent_failures"].append(dict(failure))
            else:
                context["recent_failures"].append({
                    "message": str(failure),
                    "error_type": node.metadata.get("error_type", "unknown"),
                })
        
        # Edge convention: source prerequisite -> target dependent.
        dep_ids = [
            dependency.node_id
            for dependency in self.task_graph.get_upstream_nodes(node.node_id)
        ]
        
        # Get upstream artifacts and facts
        for dep_id in dep_ids:
            dep_node = self.task_graph.get_node(dep_id)
            if dep_node and dep_node.status == "completed":
                # Collect artifacts from dependencies
                for artifact_id in self.task_graph.artifacts:
                    artifact = self.task_graph.artifacts[artifact_id]
                    if artifact.node_id == dep_id:
                        context["relevant_artifacts"].append(artifact.path)
                
                # Collect facts from dependencies
                for fact_id in self.task_graph.facts:
                    fact = self.task_graph.facts[fact_id]
                    if fact.source == dep_id:
                        context["relevant_facts"].append(fact.text)
                        # 记录 facts.retrieved 事件
                        self.trace_recorder.record("facts.retrieved", {
                            "node_id": node.node_id,
                            "fact_id": fact_id,
                            "source_node": dep_id,
                            "fact_text": fact.text[:100],  # 限制长度
                        })
        
        return context
    
    def build_task_contract(self, node: TaskNode, role: str, context: Dict, resource: str) -> Dict:
        """Build a task contract for execution.
        
        Args:
            node: The task node
            role: The assigned role
            context: The execution context
            resource: The resource tier
            
        Returns:
            Dictionary containing the task contract
        """
        return {
            "node_id": node.node_id,
            "goal": node.goal,
            "role": role,
            "context": context,
            "resource": resource,
            "acceptance_criteria": node.acceptance_criteria,
            "constraints": node.constraints,
            "inputs": node.inputs,
            "expected_outputs": node.outputs,
        }
    
    def refresh_sparse_topology(self) -> Dict:
        """Refresh the sparse communication topology.
        
        Returns:
            Dictionary containing communication plan
        """
        ready_nodes = self.select_ready_nodes()
        node_ids = [n.node_id for n in ready_nodes]
        
        communication_paths = self.determine_communication_paths(node_ids)
        optimized_order = self.optimize_task_order(node_ids)
        
        return {
            "edges": communication_paths,
            "next_nodes": optimized_order,
        }
    
    
    def _select_agent_for_task(self, node: TaskNode) -> str:
        """Select the most appropriate agent role for a task.
        
        Args:
            node: The task node to assign an agent to
            
        Returns:
            The name of the selected agent role
        """
        # Get all available roles
        available_roles = self.agent_registry.list_roles()
        
        if not available_roles:
            raise LookupError("Router cannot match a Worker because no roles are registered")
        
        # Score each role based on how well it matches the task
        role_scores = {}
        
        for role_name in available_roles:
            role = self.agent_registry.get_role(role_name)
            if role:
                score = self._calculate_role_score(role, node)
                role_scores[role_name] = score
        
        # Select the role with the highest score
        if role_scores:
            best_role = max(role_scores, key=lambda role: role_scores[role])
            return best_role
        
        raise LookupError(
            f"Router found no registered Worker role for task node {node.node_id}"
        )

    def set_communication_edge_committer(self, committer) -> None:
        """Bind the Governor-owned graph mutation boundary."""
        self.value_router.edge_committer = committer
    
    def _calculate_role_score(self, role: AgentRole, node: TaskNode) -> float:
        """Calculate a score for how well a role matches a task.
        
        Integrates HeterogeneousWorker profiles (阶段六) for capability-aware
        scoring: matches role capabilities against worker tool sets and
        resource tier preferences.
        
        Args:
            role: The agent role to score
            node: The task node to match against
            
        Returns:
            A score between 0.0 and 1.0
        """
        score = 0.0
        
        # Factor 1: Role name match (0.25 weight)
        if role.name.lower() == node.role.lower():
            score += 0.25
        elif role.name.lower() in node.role.lower() or node.role.lower() in role.name.lower():
            score += 0.125
        
        # 阶段六: HeterogeneousWorker profile matching (0.10 weight bonus)
        try:
            from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
            for wtype, wprofile in HETEROGENEOUS_WORKERS.items():
                if wtype.lower() in node.role.lower() or wtype.lower() in role.name.lower():
                    # Tool compatibility bonus
                    if wprofile.resource_tier == node.resource_tier:
                        score += 0.05
                    # Capability match bonus
                    goal_lower = node.goal.lower()
                    matching_caps = sum(1 for c in wprofile.capabilities if c.lower() in goal_lower)
                    if matching_caps > 0:
                        score += 0.05 * min(matching_caps / len(wprofile.capabilities), 1.0)
                    break  # Only match first worker type
        except Exception:
            pass
        
        # Factor 2: Goal keyword match (0.25 weight)
        goal_keywords = self._extract_keywords(node.goal)
        role_keywords = self._extract_keywords(role.description)
        
        if goal_keywords and role_keywords:
            intersection = len(goal_keywords.intersection(role_keywords))
            union = len(goal_keywords.union(role_keywords))
            jaccard_similarity = intersection / union if union > 0 else 0
            score += 0.25 * jaccard_similarity
        
        # Factor 3: Tool compatibility (0.15 weight)
        required_tools = self._infer_required_tools(node.goal)
        if required_tools and role.default_tools:
            intersection = len(set(required_tools).intersection(set(role.default_tools)))
            score += 0.15 * (intersection / len(required_tools)) if required_tools else 0
        
        # Factor 4: Capability match (0.15 weight)
        required_capabilities = self._infer_required_capabilities(node.goal)
        if required_capabilities and role.capabilities:
            intersection = len(set(required_capabilities).intersection(set(role.capabilities)))
            score += 0.15 * (intersection / len(required_capabilities)) if required_capabilities else 0
        
        # Factor 5: Graph structure (0.10 weight) - prefer roles that can handle task complexity
        dep_depth = self._calculate_dependency_depth(node)
        blocked_upstream = self._get_blocked_upstream_count(node)
        
        # Prefer shallower dependency depth (easier to execute)
        depth_score = max(0, 1.0 - (dep_depth * 0.1))  # Reduce by 0.1 per depth level
        score += 0.10 * depth_score
        
        # Penalize if there are blocked upstream nodes
        if blocked_upstream > 0:
            block_penalty = min(0.5, blocked_upstream * 0.1)  # Max 0.5 penalty
            score -= 0.05 * block_penalty
        
        # Factor 6: Risk state (0.10 weight) - avoid roles with high failure history
        failure_count = self._get_role_failure_count(role.name)
        if failure_count > 0:
            # Penalize roles with recent failures
            failure_penalty = min(1.0, failure_count * 0.2)  # Max penalty of 1.0
            score -= 0.10 * failure_penalty
        else:
            # Bonus for roles with no recent failures
            score += 0.05
        
        return max(0.0, min(score, 1.0))  # Clamp to [0.0, 1.0]
    
    def _extract_keywords(self, text: str) -> set:
        """Extract keywords from text.
        
        Args:
            text: Text to extract keywords from
            
        Returns:
            Set of keywords
        """
        # Simple keyword extraction - in a real implementation, this might use NLP techniques
        stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        words = text.lower().replace(",", "").replace(".", "").replace(";", "").split()
        return {word for word in words if word not in stop_words and len(word) > 2}
    
    def _infer_required_tools(self, goal: str) -> List[str]:
        """Infer required tools based on the goal description.
        
        Args:
            goal: The goal description
            
        Returns:
            List of inferred required tools
        """
        # Simple inference based on keywords - in a real implementation, this might use more sophisticated NLP
        goal_lower = goal.lower()
        tools = []
        
        if "code" in goal_lower or "program" in goal_lower or "implement" in goal_lower:
            tools.extend(["terminal", "file_editor", "code_runner"])
        
        if "test" in goal_lower:
            tools.extend(["terminal", "test_runner"])
        
        if "research" in goal_lower or "study" in goal_lower:
            tools.extend(["web_search", "document_parser"])
        
        if "document" in goal_lower or "report" in goal_lower:
            tools.extend(["file_editor", "document_generator"])
        
        return list(set(tools))  # Remove duplicates
    
    def _infer_required_capabilities(self, goal: str) -> List[str]:
        """Infer required capabilities based on the goal description.
        
        Args:
            goal: The goal description
            
        Returns:
            List of inferred required capabilities
        """
        # Simple inference based on keywords
        goal_lower = goal.lower()
        capabilities = []
        
        if "code" in goal_lower or "program" in goal_lower:
            capabilities.append("programming")
        
        if "test" in goal_lower:
            capabilities.append("testing")
        
        if "research" in goal_lower or "study" in goal_lower:
            capabilities.append("research")
        
        if "analyze" in goal_lower or "analysis" in goal_lower:
            capabilities.append("analysis")
        
        if "design" in goal_lower:
            capabilities.append("design")
        
        if "document" in goal_lower or "report" in goal_lower:
            capabilities.append("documentation")
        
        return list(set(capabilities))  # Remove duplicates
    
    # ------------------------------------------------------------------
    # Graph structure and risk state features (原方案书 §5.2)
    # ------------------------------------------------------------------
    
    def _calculate_risk_score(self, node: TaskNode) -> float:
        """Calculate a risk score for a node.
        
        Args:
            node: The task node
            
        Returns:
            Risk score between 0.0 and 1.0
        """
        score = 0.0
        
        # Base risk from risk_level
        if node.risk_level == "high":
            score += 0.5
        elif node.risk_level == "medium":
            score += 0.3
        else:  # low
            score += 0.1
        
        # Add risk from blocked upstream
        blocked = self._get_blocked_upstream_count(node)
        score += min(0.3, blocked * 0.1)
        
        # Add risk from retry count
        score += min(0.2, node.retry_count * 0.05)
        
        return min(1.0, score)
    
    def _calculate_dependency_depth(self, node: TaskNode) -> int:
        """Calculate the dependency depth of a node in the task graph.
        
        Args:
            node: The task node
            
        Returns:
            Maximum depth from root to this node
        """
        if not node.dependencies:
            return 0
        
        max_depth = 0
        for dep_id in node.dependencies:
            dep_node = self.task_graph.get_node(dep_id)
            if dep_node:
                depth = 1 + self._calculate_dependency_depth(dep_node)
                max_depth = max(max_depth, depth)
        
        return max_depth
    
    def _get_role_failure_count(self, role_name: str) -> int:
        """Get the historical failure count for a role.
        
        Args:
            role_name: The role name
            
        Returns:
            Number of recent failures for this role
        """
        if not self.trace_recorder:
            return 0
        
        # Count failures from trace events
        failure_count = 0
        for event in getattr(self.trace_recorder, 'events', []):
            if (event.get('event_type') == 'task.execution.failed' 
                and event.get('payload', {}).get('role') == role_name):
                failure_count += 1
        
        return failure_count
    
    def _get_blocked_upstream_count(self, node: TaskNode) -> int:
        """Count how many upstream nodes are blocked or failed.
        
        Args:
            node: The task node
            
        Returns:
            Number of blocked/failed upstream nodes
        """
        blocked_count = 0
        for dep_id in node.dependencies:
            dep_node = self.task_graph.get_node(dep_id)
            if dep_node and dep_node.status in ('blocked', 'failed'):
                blocked_count += 1
        
        return blocked_count
    
    # ------------------------------------------------------------------
    # Communication paths (原方案书 §5.2 稀疏通信边)
    #
    # 算法：基于任务依赖图构建加权有向图，使用关键路径（Critical Path Method）
    # 生成稀疏通信拓扑。仅保留必要通信边：
    #   1. 直接依赖边 (depends_on)
    #   2. 证据共享边 (evidence_for)
    #   3. 验证边 (verifies)
    #   4. 关键路径上的传递依赖边（通过最长路径算法识别）
    # 去除冗余边：若 A→B→C 存在，则 A→C 为冗余边，移除。
    # ------------------------------------------------------------------

    def route_value_message(
        self,
        sender: str,
        receiver: str,
        msg_type: str,
        task_id: str,
        claims: List[Dict[str, Any]] | None = None,
        evidence_refs: List[str] | None = None,
        open_questions: List[str] | None = None,
        information_gain: float = 0.5,
        relevance: float = 0.5,
        urgency: float = 0.5,
    ) -> bool:
        """Route a structured message through value-based filtering.
        
        Implements IGRC (Information-Gain Routing and Compression) algorithm.
        Only transmits if V(m) = I × R × U - C exceeds threshold.
        
        Args:
            sender: Source node ID
            receiver: Target node ID
            msg_type: Message type (evidence_update, conflict_alert, etc.)
            task_id: Associated task ID
            claims: Structured claims
            evidence_refs: Evidence reference IDs
            open_questions: Unresolved questions
            information_gain: Information novelty score
            relevance: Relevance to receiver
            urgency: Urgency score
            
        Returns:
            True if message was transmitted, False if filtered out
        """
        import time as _time
        from AstralNexus.core.value_router import RoutedMessageEnvelope
        self.value_router._message_counter += 1
        msg = RoutedMessageEnvelope(
            message_id=f"msg-{self.value_router._message_counter:06d}",
            sender=sender,
            receiver=receiver,
            msg_type=msg_type,
            task_id=task_id,
            claims=claims or [],
            evidence_refs=evidence_refs or [],
            open_questions=open_questions or [],
            information_gain=information_gain,
            relevance=relevance,
            urgency=urgency,
            timestamp=_time.time(),
        )
        transmitted = self.value_router.route_message(msg)
        
        if self.trace_recorder:
            self.trace_recorder.record("router.value_routing", {
                "message_id": msg.message_id,
                "sender": sender,
                "receiver": receiver,
                "type": msg_type,
                "value": msg.compute_value(),
                "transmitted": transmitted,
            })
        
        return transmitted
    
    def get_value_routing_stats(self) -> Dict[str, Any]:
        """Get value routing statistics for evaluation reporting."""
        return self.value_router.get_routing_stats()
    
    def determine_communication_paths(self, node_ids: List[str]) -> List[Tuple[str, str]]:
        """Determine sparse communication paths using Critical Path Method.

        Builds a weighted dependency graph, computes longest paths (critical paths),
        then prunes redundant transitive edges to produce a minimal spanning subgraph.

        Args:
            node_ids: List of node IDs to establish communication between

        Returns:
            List of (source, target) tuples — the minimal necessary communication edges
        """
        node_set = set(node_ids)
        if not node_set:
            return []

        # Step 1: Collect all edges among active nodes, categorised by type
        direct_edges: List[Tuple[str, str, str]] = []  # (src, tgt, edge_type)
        for edge in self.task_graph.edges:
            if edge.source in node_set and edge.target in node_set:
                direct_edges.append((edge.source, edge.target, edge.edge_type))

        # Step 2: Build adjacency and reverse adjacency for transitive reduction
        adj: Dict[str, set] = {nid: set() for nid in node_ids}
        for src, tgt, _ in direct_edges:
            adj[src].add(tgt)

        # Step 3: Transitive reduction — remove edge A→C if path A→B→…→C exists
        #          via another intermediate node (Kruskal-style redundancy pruning).
        essential_edges: set = set()
        for src, tgt, etype in direct_edges:
            # Check if tgt is reachable from src via any path NOT using the direct edge
            reachable_without_direct = self._bfs_reachable(
                adj, src, tgt, skip_edge=(src, tgt), node_set=node_set
            )
            if not reachable_without_direct:
                # Edge is essential — no alternative path exists
                essential_edges.add((src, tgt))

        # Step 4: Add critical-path edges — for nodes on the critical path,
        #          ensure all direct predecessors on that path are connected.
        #          Compute longest-path distances from each root node.
        depths = self._compute_critical_path_depths(node_ids, adj)

        # For nodes with depth > 0, ensure at least one parent edge is kept
        # even after transitive reduction (critical path preservation).
        for nid in node_ids:
            if depths.get(nid, 0) > 0:
                # Find the deepest predecessor
                best_parent = None
                best_depth = -1
                for edge in self.task_graph.edges:
                    if (edge.target == nid and edge.source in node_set
                            and edge.edge_type == "depends_on"):
                        parent_depth = depths.get(edge.source, 0)
                        if parent_depth > best_depth:
                            best_depth = parent_depth
                            best_parent = edge.source
                if best_parent:
                    essential_edges.add((best_parent, nid))

        return sorted(essential_edges)

    def _bfs_reachable(
        self,
        adj: Dict[str, set],
        source: str,
        target: str,
        skip_edge: Tuple[str, str],
        node_set: set,
    ) -> bool:
        """BFS from source to target, skipping the direct edge (source, target).

        Returns True if target is still reachable via an alternate path.
        """
        visited: set = set()
        queue = [source]
        while queue:
            current = queue.pop(0)
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            for neighbour in adj.get(current, set()):
                if neighbour not in visited and (current, neighbour) != skip_edge:
                    if neighbour in node_set:
                        queue.append(neighbour)
        return False

    def _compute_critical_path_depths(
        self, node_ids: List[str], adj: Dict[str, set]
    ) -> Dict[str, int]:
        """Compute longest-path depth for each node (critical path method).

        Nodes with no predecessors have depth 0. Each other node's depth is
        1 + max(depth of predecessors).

        Returns:
            Dictionary mapping node_id -> depth
        """
        # Build reverse adjacency (predecessors)
        preds: Dict[str, List[str]] = {nid: [] for nid in node_ids}
        for src, targets in adj.items():
            for tgt in targets:
                if tgt in preds:
                    preds[tgt].append(src)

        depths: Dict[str, int] = {}

        def _depth(nid: str) -> int:
            if nid in depths:
                return depths[nid]
            if not preds[nid]:
                depths[nid] = 0
                return 0
            d = 1 + max(_depth(p) for p in preds[nid])
            depths[nid] = d
            return d

        for nid in node_ids:
            _depth(nid)

        return depths

    # ------------------------------------------------------------------
    # Semantic similarity sparse routing (语义稀疏路由)
    # ------------------------------------------------------------------

    def compute_semantic_edges(
        self, node_ids: List[str], *, threshold: float = 0.3, max_extra: int = 5
    ) -> List[Tuple[str, str, float]]:
        """Compute weighted communication edges using multi-factor scoring.

        Each candidate edge gets a composite weight from four factors:
          1. **Semantic similarity** (w=0.35) — TF-IDF cosine between goals
          2. **Dependency strength** (w=0.25) — structural proximity in DAG
          3. **Role capability match** (w=0.20) — how well the roles complement
          4. **Historical contribution** (w=0.20) — past collaboration success rate

        Returns edges sorted by composite weight descending, capped at max_extra.
        """
        if len(node_ids) < 2:
            return []

        nodes = {nid: node for nid in node_ids
                 if (node := self.task_graph.get_node(nid)) is not None}
        if len(nodes) < 2:
            return []

        # Pre-compute TF-IDF vectors for semantic factor
        corpus = [(nid, node.goal) for nid, node in nodes.items()]
        tokenised = [(nid, self._tokenise_goal(goal)) for nid, goal in corpus]
        n = len(tokenised)
        doc_freq: Counter = Counter()
        for _, tokens in tokenised:
            for tok in set(tokens):
                doc_freq[tok] += 1
        idf = {tok: math.log((n + 1) / (df + 1)) + 1 for tok, df in doc_freq.items()}
        vectors: Dict[str, Dict[str, float]] = {}
        for nid, tokens in tokenised:
            tf = Counter(tokens)
            total = len(tokens) or 1
            vectors[nid] = {tok: (tf[tok] / total) * idf.get(tok, 1.0) for tok in tf}

        # Pre-compute structural distances for dependency factor
        adj: Dict[str, set] = {nid: set() for nid in node_ids}
        for edge in self.task_graph.edges:
            if edge.source in nodes and edge.target in nodes:
                adj[edge.source].add(edge.target)

        existing_edges = set()
        for edge in self.task_graph.edges:
            existing_edges.add((edge.source, edge.target))
            existing_edges.add((edge.target, edge.source))

        # Score all non-adjacent pairs
        candidates: List[Tuple[str, str, float]] = []
        nids = list(nodes.keys())
        for i in range(len(nids)):
            for j in range(i + 1, len(nids)):
                nid_a, nid_b = nids[i], nids[j]
                if (nid_a, nid_b) in existing_edges:
                    continue

                # Factor 1: semantic similarity (0-1)
                sem_sim = self._cosine_similarity(vectors.get(nid_a, {}), vectors.get(nid_b, {}))

                # Factor 2: dependency proximity (0-1) — inverse graph distance
                dist = self._bfs_distance(adj, nid_a, nid_b, set(nodes.keys()))
                dep_score = 1.0 / (1.0 + dist) if dist > 0 else 0.0

                # Factor 3: capability complementarity from the live Worker
                # profiles. Static role-pair tables are not a dynamic topology.
                capability_score = self._capability_complementarity(
                    nodes[nid_a], nodes[nid_b]
                )

                weight = (
                    0.45 * sem_sim
                    + 0.30 * dep_score
                    + 0.25 * capability_score
                )

                if weight >= threshold:
                    candidates.append((nid_a, nid_b, round(weight, 4)))

        candidates.sort(key=lambda x: -x[2])
        return candidates[:max_extra]

    def build_communication_plan(
        self, node_ids: List[str]
    ) -> Dict[str, Any]:
        """Build a communication plan for the given nodes.

        Public entry-point used by Orchestrator's ``_calculate_phase2_metrics()``
        to obtain real topology metrics (semantic edges, dependency edges,
        sparsity, augmentation ratio) directly from the Router — **not** from a
        fallback path.

        Internally delegates to :meth:`build_full_routing_topology`.

        Args:
            node_ids: Active node IDs

        Returns:
            Dict with keys: dependency_edges, semantic_edges, all_edges, metrics
        """
        return self.build_full_routing_topology(node_ids)

    def build_full_routing_topology(
        self, node_ids: List[str]
    ) -> Dict[str, Any]:
        """Build the complete routing topology combining dependency + semantic edges.

        Returns a unified topology dict with both structural and semantic edges,
        suitable for trace recording and downstream communication planning.

        Args:
            node_ids: Active node IDs

        Returns:
            Dict with keys: dependency_edges, semantic_edges, all_edges, metrics
        """
        dep_edges = self.determine_communication_paths(node_ids)
        sem_edges = self.compute_semantic_edges(node_ids)

        all_edges = set(dep_edges)
        for src, tgt, _score in sem_edges:
            all_edges.add((src, tgt))

        total_nodes = len(node_ids)
        full_edges = total_nodes * (total_nodes - 1) / 2 if total_nodes > 1 else 0

        return {
            "dependency_edges": dep_edges,
            "semantic_edges": [(s, t, sc) for s, t, sc in sem_edges],
            "all_edges": sorted(all_edges),
            "metrics": {
                "total_nodes": total_nodes,
                "dependency_edge_count": len(dep_edges),
                "semantic_edge_count": len(sem_edges),
                "total_edge_count": len(all_edges),
                "full_connection_edges": int(full_edges),
                "sparsity": 1 - len(all_edges) / full_edges if full_edges > 0 else 1.0,
                "semantic_augmentation_ratio": (
                    len(sem_edges) / len(dep_edges) if dep_edges else 0
                ),
            },
        }

    @staticmethod
    def _tokenise_goal(goal: str) -> List[str]:
        """Tokenise a goal string for TF-IDF (handles CJK + Latin)."""
        # Split on whitespace and punctuation
        tokens = re.findall(r'[a-zA-Z_]\w*|[\u4e00-\u9fff]{1,4}', goal.lower())
        # Remove very short tokens
        return [t for t in tokens if len(t) >= 2]

    @staticmethod
    def _cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
        """Compute cosine similarity between two sparse TF-IDF vectors."""
        if not a or not b:
            return 0.0
        common_keys = a.keys() & b.keys()
        if not common_keys:
            return 0.0
        dot = sum(a[k] * b[k] for k in common_keys)
        mag_a = math.sqrt(sum(v * v for v in a.values()))
        mag_b = math.sqrt(sum(v * v for v in b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    @staticmethod
    def _bfs_distance(adj: Dict[str, set], src: str, tgt: str, node_set: set) -> int:
        """Compute shortest path distance from src to tgt in directed graph."""
        if src == tgt:
            return 0
        visited = {src}
        queue = [(src, 0)]
        while queue:
            current, dist = queue.pop(0)
            for neighbor in adj.get(current, set()):
                if neighbor == tgt:
                    return dist + 1
                if neighbor in node_set and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, dist + 1))
        return -1  # unreachable

    @staticmethod
    def _capability_complementarity(node_a: TaskNode, node_b: TaskNode) -> float:
        """Score shared context plus heterogeneous capability coverage."""
        profile_a = node_a.metadata.get("worker_profile") or {}
        profile_b = node_b.metadata.get("worker_profile") or {}
        caps_a = {
            str(item).strip().lower()
            for item in profile_a.get("capabilities", [])
            if str(item).strip()
        }
        caps_b = {
            str(item).strip().lower()
            for item in profile_b.get("capabilities", [])
            if str(item).strip()
        }
        union = caps_a | caps_b
        if not union:
            return 0.0
        shared = len(caps_a & caps_b) / len(union)
        diversity = len(caps_a ^ caps_b) / len(union)
        return 0.6 * shared + 0.4 * diversity

    def optimize_task_order(self, node_ids: List[str]) -> List[str]:
        """Optimize task execution order using critical-path-aware topological sort.

        Algorithm:
          1. Compute critical-path depths (longest path from roots)
          2. Topological sort, breaking ties by critical-path depth (deepest first)
             to prioritise tasks on the critical path
          3. Within same depth, sort by risk score (high-risk first, to surface
             failures early)

        Args:
            node_ids: List of node IDs to order

        Returns:
            Optimized list of node IDs
        """
        node_set = set(node_ids)

        # Build dependency graph
        dependencies: Dict[str, set] = {nid: set() for nid in node_ids}
        dependents: Dict[str, set] = {nid: set() for nid in node_ids}

        for edge in self.task_graph.edges:
            if edge.source in node_set and edge.target in node_set:
                if edge.edge_type == "depends_on":
                    dependencies[edge.target].add(edge.source)
                    dependents[edge.source].add(edge.target)

        # Compute critical path depths
        fwd_adj: Dict[str, set] = {nid: set() for nid in node_ids}
        for nid, deps in dependents.items():
            fwd_adj[nid] = deps
        depths = self._compute_critical_path_depths(node_ids, fwd_adj)

        # Kahn's algorithm with priority: higher depth first, then higher risk
        ordered: List[str] = []
        ready = [nid for nid in node_ids if not dependencies[nid]]

        while ready:
            # Sort ready nodes: critical-path depth descending, risk descending
            ready.sort(key=lambda nid: (-depths.get(nid, 0),
                                        -self._calculate_risk_score(node)
                                        if (node := self.task_graph.get_node(nid)) is not None else 0))

            current = ready.pop(0)
            ordered.append(current)

            for dep in dependents[current]:
                dependencies[dep].discard(current)
                if not dependencies[dep]:
                    ready.append(dep)

        # Append any remaining nodes (cycles — shouldn't happen in a valid DAG)
        remaining = set(node_ids) - set(ordered)
        ordered.extend(sorted(remaining))

        return ordered


