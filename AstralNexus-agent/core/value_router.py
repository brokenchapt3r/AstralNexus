"""Value-based Message Router: V(m) = I(m) × R(m) × U(m) - C(m)

Implements the value routing mechanism from the advanced spec:
- Each message has a computed value score
- Only messages exceeding threshold are transmitted
- Prevents broadcast storms and information noise
- Implements low-entropy communication protocol

Dynamic edge creation conditions (5 types):
1. Explicit dependency (B depends on A)
2. Resolution (A's result resolves B's open question)
3. Conflict (A discovers conflict with B)
4. Capability request (B requests capability that A provides)
5. Value threshold (information value exceeds threshold)
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from AstralNexus.core.task_graph import TaskGraph

logger = logging.getLogger(__name__)


@dataclass
class RoutedMessageEnvelope:
    """Value-routed envelope for sparse inter-agent communication.

    This is deliberately distinct from
    ``message_protocol.StructuredMessage``, which represents a worker result.
    """
    message_id: str
    sender: str
    receiver: str
    msg_type: str  # evidence_update, capability_request, conflict_alert, resolution, status_delta
    task_id: str
    
    # Core content (structured, not free text)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    recommended_action: Optional[str] = None
    
    # Value components
    information_gain: float = 0.5
    relevance: float = 0.5
    urgency: float = 0.5
    transmission_cost: float = 0.1
    
    # Metadata
    confidence: float = 0.8
    ttl: int = 3  # hops remaining
    timestamp: float = field(default_factory=time.time)
    
    def compute_value(self) -> float:
        """Compute message value: V(m) = I(m) × R(m) × U(m) - C(m)"""
        return (
            self.information_gain * self.relevance * self.urgency
            - self.transmission_cost
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for transmission."""
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "type": self.msg_type,
            "task_id": self.task_id,
            "claims": self.claims,
            "evidence_refs": self.evidence_refs,
            "open_questions": self.open_questions,
            "recommended_action": self.recommended_action,
            "value": self.compute_value(),
            "confidence": self.confidence,
            "ttl": self.ttl,
        }


class EdgeCondition(str):
    """Dynamic edge creation conditions."""
    EXPLICIT_DEPENDENCY = "explicit_dependency"
    RESOLUTION = "resolution"  # A resolves B's open question
    CONFLICT = "conflict"  # A discovers conflict with B
    CAPABILITY_REQUEST = "capability_request"  # B requests A's capability
    VALUE_THRESHOLD = "value_threshold"  # Info value exceeds threshold


@dataclass
class EdgeProposal:
    """A proposal to create a new edge."""
    source: str
    target: str
    condition: str
    value_score: float
    reasoning: str
    edge_type: str = "dynamic"


class ValueRouter:
    """Routes messages based on value computation and dynamic edge creation.
    
    Key principles:
    1. Messages are structured (not full conversation dumps)
    2. Each message has a value score V(m) = I × R × U - C
    3. Only high-value messages are transmitted
    4. Edges are created dynamically based on 5 conditions
    5. Semantic deduplication prevents redundant messages
    """
    
    def __init__(
        self,
        task_graph: TaskGraph,
        value_threshold: float = 0.3,
        semantic_similarity_threshold: float = 0.85,
        edge_committer: Optional[Callable[[EdgeProposal], None]] = None,
    ):
        self.task_graph = task_graph
        self.value_threshold = value_threshold
        self.semantic_similarity_threshold = semantic_similarity_threshold
        self.edge_committer = edge_committer
        self._message_counter = 0
        self._transmitted_messages: List[Dict[str, Any]] = []
        self._dropped_messages: List[Dict[str, Any]] = []
        self._dynamic_edges: List[Dict[str, Any]] = []
        self._evidence_base: Dict[str, Any] = {}  # For semantic dedup
    
    def route_message(self, message: RoutedMessageEnvelope) -> bool:
        """Route a message if its value exceeds threshold.
        
        Args:
            message: Structured message to route
            
        Returns:
            True if message was transmitted, False if dropped
        """
        # Compute value
        value = message.compute_value()
        
        # Check semantic dedup
        if self._is_duplicate(message):
            self._dropped_messages.append({
                "message_id": message.message_id,
                "reason": "semantic_duplicate",
                "value": value,
            })
            return False
        
        # Check value threshold
        if value < self.value_threshold:
            self._dropped_messages.append({
                "message_id": message.message_id,
                "reason": "value_below_threshold",
                "value": value,
                "threshold": self.value_threshold,
            })
            return False
        
        # Check TTL
        if message.ttl <= 0:
            self._dropped_messages.append({
                "message_id": message.message_id,
                "reason": "ttl_expired",
                "value": value,
            })
            return False
        
        # Evaluate dynamic edge creation
        edge_proposal = self._evaluate_edge_creation(message)
        if edge_proposal:
            self._create_dynamic_edge(edge_proposal)
        
        # Transmit
        self._transmitted_messages.append({
            "message_id": message.message_id,
            "sender": message.sender,
            "receiver": message.receiver,
            "value": value,
            "type": message.msg_type,
            "timestamp": message.timestamp,
        })
        
        # Update evidence base for dedup
        self._update_evidence_base(message)
        
        logger.debug(f"Transmitted message {message.message_id}: value={value:.3f}")
        return True
    
    def _is_duplicate(self, message: RoutedMessageEnvelope) -> bool:
        """Check if message is semantically duplicate of existing evidence."""
        # Simple heuristic: check claim overlap
        if not message.claims:
            return False
        
        for claim in message.claims:
            claim_text = claim.get("claim", "")
            for existing in self._evidence_base.values():
                if self._text_similarity(claim_text, existing.get("claim", "")) > self.semantic_similarity_threshold:
                    return True
        return False
    
    def _text_similarity(self, text1: str, text2: str) -> float:
        """Simple text similarity (Jaccard on words)."""
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        if not words1 or not words2:
            return 0.0
        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)
    
    def _evaluate_edge_creation(self, message: RoutedMessageEnvelope) -> Optional[EdgeProposal]:
        """Evaluate if a dynamic edge should be created based on 5 conditions."""
        sender_node = self.task_graph.get_node(message.sender)
        receiver_node = self.task_graph.get_node(message.receiver)
        
        if not sender_node or not receiver_node:
            return None
        
        # Condition 1: Explicit dependency (already handled by task_graph.add_edge)
        # Skip - this is handled at spawn time
        
        # Condition 2: Resolution - A's result resolves B's open question
        if message.msg_type == "resolution" and message.open_questions:
            value = message.compute_value()
            return EdgeProposal(
                source=message.sender,
                target=message.receiver,
                condition=EdgeCondition.RESOLUTION,
                value_score=value,
                reasoning=f"Resolves open questions: {message.open_questions}",
                edge_type="resolves",
            )
        
        # Condition 3: Conflict - A discovers conflict with B
        if message.msg_type == "conflict_alert":
            value = message.compute_value()
            return EdgeProposal(
                source=message.sender,
                target=message.receiver,
                condition=EdgeCondition.CONFLICT,
                value_score=value,
                reasoning=f"Conflict detected: {message.claims}",
                edge_type="conflicts_with",
            )
        
        # Condition 4: Capability request - B requests A's capability
        if message.msg_type == "capability_request":
            value = message.compute_value()
            return EdgeProposal(
                source=message.sender,
                target=message.receiver,
                condition=EdgeCondition.CAPABILITY_REQUEST,
                value_score=value,
                reasoning=f"Capability requested: {message.recommended_action}",
                edge_type="provides_capability",
            )
        
        # Condition 5: Value threshold - high-value information flow
        value = message.compute_value()
        if value > self.value_threshold * 1.5:  # Higher threshold for edge creation
            return EdgeProposal(
                source=message.sender,
                target=message.receiver,
                condition=EdgeCondition.VALUE_THRESHOLD,
                value_score=value,
                reasoning=f"High value flow: {value:.3f}",
                edge_type="information_flow",
            )
        
        return None
    
    def _create_dynamic_edge(self, proposal: EdgeProposal) -> None:
        """Submit a communication-edge proposal to the graph authority."""
        # Check if edge already exists
        for edge in self.task_graph.edges:
            if (
                edge.source == proposal.source
                and edge.target == proposal.target
                and edge.edge_type == "communicates_to"
            ):
                return
        
        if self.edge_committer is None:
            raise RuntimeError(
                "ValueRouter has no Governor graph-patch committer"
            )
        self.edge_committer(proposal)
        
        self._dynamic_edges.append({
            "source": proposal.source,
            "target": proposal.target,
            "condition": proposal.condition,
            "value_score": proposal.value_score,
            "reasoning": proposal.reasoning,
            "edge_type": proposal.edge_type,
            "timestamp": time.time(),
        })
        
        logger.info(f"Created dynamic edge: {proposal.source} -> {proposal.target} ({proposal.condition})")
    
    def _update_evidence_base(self, message: RoutedMessageEnvelope) -> None:
        """Update evidence base for semantic dedup."""
        for claim in message.claims:
            evidence_id = f"E-{len(self._evidence_base) + 1}"
            self._evidence_base[evidence_id] = {
                "claim": claim.get("claim", ""),
                "source": message.sender,
                "task_id": message.task_id,
                "confidence": claim.get("confidence", 0.8),
                "timestamp": message.timestamp,
            }
    
    def get_routing_stats(self) -> Dict[str, Any]:
        """Get message routing statistics."""
        values = [m["value"] for m in self._transmitted_messages]
        
        return {
            "total_transmitted": len(self._transmitted_messages),
            "total_dropped": len(self._dropped_messages),
            "transmission_rate": (
                len(self._transmitted_messages) /
                max(1, len(self._transmitted_messages) + len(self._dropped_messages))
            ),
            "avg_value": sum(values) / max(1, len(values)),
            "max_value": max(values) if values else 0.0,
            "min_value": min(values) if values else 0.0,
            "drop_reasons": self._count_drop_reasons(),
            "dynamic_edges_created": len(self._dynamic_edges),
            "edge_conditions": self._count_edge_conditions(),
            "evidence_base_size": len(self._evidence_base),
        }
    
    def _count_drop_reasons(self) -> Dict[str, int]:
        """Count reasons for dropping messages."""
        reasons = {}
        for msg in self._dropped_messages:
            r = msg["reason"]
            reasons[r] = reasons.get(r, 0) + 1
        return reasons
    
    def _count_edge_conditions(self) -> Dict[str, int]:
        """Count edge creation conditions."""
        conditions = {}
        for edge in self._dynamic_edges:
            c = edge["condition"]
            conditions[c] = conditions.get(c, 0) + 1
        return conditions
