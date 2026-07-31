from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from AstralNexus.core.state_machine import TaskStatus

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


@dataclass
class StructuredMessage:
    """Structured message protocol for low-entropy communication between agents."""
    
    # Node identifier this message relates to
    node_id: str
    
    # Role that generated this message
    role: str
    
    # Status of the task
    status: TaskStatus
    
    # Brief summary of the result (max 120 characters)
    summary: str
    
    # Claims made with evidence and confidence
    claims: List[Dict[str, Any]] = field(default_factory=list)
    
    # Artifacts produced
    artifacts: List[str] = field(default_factory=list)
    
    # Risks identified
    risks: List[str] = field(default_factory=list)
    
    # Next actions suggested
    next_actions: List[Dict[str, Any]] = field(default_factory=list)
    
    # Budget used for this task
    budget_used: Dict[str, int] = field(default_factory=dict)
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "status" and not isinstance(value, TaskStatus):
            value = TaskStatus(str(value))
        super().__setattr__(name, value)


class MessageProtocol:
    """Handles the creation, validation, and processing of structured messages."""
    
    def __init__(self):
        # Schema for validating structured messages
        self.schema = {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "role": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [status.value for status in TaskStatus],
                },
                "summary": {"type": "string", "maxLength": 120},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
                        },
                        "required": ["text", "evidence", "confidence"]
                    }
                },
                "artifacts": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "next_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["spawn", "revise", "verify", "finish"]},
                            "target": {"type": "string"},
                            "reason": {"type": "string"}
                        },
                        "required": ["type", "target", "reason"]
                    }
                },
                "budget_used": {
                    "type": "object",
                    "properties": {
                        "prompt_tokens": {"type": "integer", "minimum": 0},
                        "completion_tokens": {"type": "integer", "minimum": 0},
                        "wall_time_s": {"type": "number", "minimum": 0}
                    }
                },
                "metadata": {"type": "object"}
            },
            "required": ["node_id", "role", "status", "summary"]
        }
    
    def create_message(self, **kwargs) -> StructuredMessage:
        """Create a structured message with the given parameters.
        
        Args:
            **kwargs: Parameters for the message
            
        Returns:
            StructuredMessage instance
        """
        return StructuredMessage(**kwargs)
    
    def validate_message(self, message: StructuredMessage) -> tuple[bool, Optional[str]]:
        """Validate a structured message against the schema.
        
        Args:
            message: The message to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        message_dict = asdict(message)
        
        if not HAS_JSONSCHEMA:
            # Fallback to manual validation when jsonschema is not installed
            return self._validate_message_manual(message_dict)
        
        try:
            jsonschema.validate(instance=message_dict, schema=self.schema)
            return True, None
        except jsonschema.ValidationError as e:
            return False, f"Validation error: {e.message}"
        except jsonschema.SchemaError as e:
            return False, f"Schema error: {e.message}"
    
    def _validate_message_manual(self, message_dict: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Manual validation fallback when jsonschema is not available.
        
        Args:
            message_dict: Message as dictionary
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Required fields
        for field_name in ["node_id", "role", "status", "summary"]:
            if not message_dict.get(field_name):
                return False, f"Missing required field: {field_name}"
        
        # Status enum check
        if message_dict["status"] not in {status.value for status in TaskStatus}:
            return False, f"Invalid status: {message_dict['status']}"
        
        # Summary length
        if len(message_dict.get("summary", "")) > 120:
            return False, "Summary exceeds 120 characters"
        
        # Claims validation
        for i, claim in enumerate(message_dict.get("claims", [])):
            if "text" not in claim:
                return False, f"Claim {i} missing text"
            if "evidence" not in claim:
                return False, f"Claim {i} missing evidence"
            if "confidence" not in claim:
                return False, f"Claim {i} missing confidence"
            conf = claim["confidence"]
            if not isinstance(conf, (int, float)) or conf < 0.0 or conf > 1.0:
                return False, f"Claim {i} invalid confidence: {conf}"
        
        # Next actions validation
        for i, action in enumerate(message_dict.get("next_actions", [])):
            if action.get("type") not in ["spawn", "revise", "verify", "finish"]:
                return False, f"Next action {i} invalid type: {action.get('type')}"
            if "target" not in action:
                return False, f"Next action {i} missing target"
            if "reason" not in action:
                return False, f"Next action {i} missing reason"
        
        # Budget validation
        budget = message_dict.get("budget_used", {})
        for key in ["prompt_tokens", "completion_tokens"]:
            if key in budget and (not isinstance(budget[key], int) or budget[key] < 0):
                return False, f"Invalid {key}: {budget[key]}"
        if "wall_time_s" in budget and (not isinstance(budget["wall_time_s"], (int, float)) or budget["wall_time_s"] < 0):
            return False, f"Invalid wall_time_s: {budget['wall_time_s']}"
        
        return True, None
    
    def validate_message_cross_references(
        self, 
        message: StructuredMessage,
        artifacts: List[str],
        task_graph: 'TaskGraph'
    ) -> tuple[bool, List[str]]:
        """Validate cross-references in a structured message.
        
        This validates:
        1. Claims evidence references exist in message artifacts
        2. Next actions target valid nodes in task graph
        3. Status transitions follow state machine rules
        
        Args:
            message: The message to validate
            artifacts: List of artifact IDs available in the message
            task_graph: Task graph to validate node references against
            
        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []
        
        # 1. Validate claims evidence references
        for i, claim in enumerate(message.claims):
            for j, evidence_ref in enumerate(claim.get("evidence", [])):
                if evidence_ref not in artifacts:
                    errors.append(
                        f"Claim {i} evidence {j} references non-existent artifact: {evidence_ref}"
                    )
        
        # 2. Validate next actions target nodes (using 'target' field from schema)
        for i, action in enumerate(message.next_actions):
            target_node_id = action.get("target")  # Schema uses 'target', not 'target_node_id'
            if target_node_id:
                # A spawn target is a proposed task description. It is
                # intentionally absent from the graph until DynamicSpawner
                # admits it; existing-node validation applies to revise,
                # verify, and finish actions only.
                if action.get("type") == "spawn":
                    continue
                if not task_graph.has_node(target_node_id):
                    errors.append(
                        f"Next action {i} references non-existent node: {target_node_id}"
                    )
        
        # 3. Validate status transitions using node context
        node = task_graph.get_node(message.node_id) if task_graph else None
        if node:
            valid_transitions = self._valid_state_transitions(node.status)
            if message.status not in valid_transitions:
                errors.append(
                    f"Invalid transition: {node.status} → {message.status}. "
                    f"Allowed: {valid_transitions}"
                )

        return len(errors) == 0, errors

    @staticmethod
    def _valid_state_transitions(current_status: str) -> List[str]:
        """Return the set of valid next statuses from the current state.
        
        Implements the AstralNexus 12-state machine:
          proposed → admitted, cancelled
          admitted → waiting_dependencies, ready, cancelled
          waiting_dependencies → ready, cancelled
          ready → running, blocked, cancelled
          running → result_submitted, failed, blocked, cancelled
          result_submitted → verifying, failed, cancelled
          verifying → completed, repair_pending, failed, cancelled
          repair_pending → ready, running, failed, cancelled
          failed → repair_pending, ready, running, blocked, cancelled
          blocked → ready, running, cancelled
          completed → superseded (terminal success)
          cancelled → (terminal)
          superseded → (terminal)
        """
        transitions: Dict[str, List[str]] = {
            "proposed": ["admitted", "cancelled"],
            "admitted": ["waiting_dependencies", "ready", "cancelled"],
            "waiting_dependencies": ["ready", "cancelled"],
            "ready": ["running", "blocked", "cancelled"],
            "running": ["result_submitted", "failed", "blocked", "cancelled"],
            "result_submitted": ["verifying", "failed", "cancelled"],
            "verifying": ["completed", "repair_pending", "failed", "cancelled"],
            "repair_pending": ["ready", "running", "failed", "cancelled"],
            "failed": ["repair_pending", "ready", "running", "blocked", "cancelled"],
            "blocked": ["ready", "running", "cancelled"],
            "completed": ["completed"],  # terminal success
            "cancelled": ["cancelled"],  # terminal
            "superseded": ["superseded"],  # terminal
        }
        return transitions.get(current_status, [current_status])
    
    def serialize_message(self, message: StructuredMessage) -> str:
        """Serialize a structured message to JSON string.
        
        Args:
            message: The message to serialize
            
        Returns:
            JSON string representation of the message
        """
        return json.dumps(asdict(message), ensure_ascii=False, indent=2)
    
    def deserialize_message(self, message_str: str) -> StructuredMessage:
        """Deserialize a JSON string to a structured message.
        
        Args:
            message_str: JSON string representation of the message
            
        Returns:
            StructuredMessage instance
            
        Raises:
            ValueError: If the JSON is invalid or doesn't match the expected structure
        """
        try:
            message_dict = json.loads(message_str)
            return StructuredMessage(**message_dict)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        except TypeError as e:
            raise ValueError(f"Invalid message structure: {e}")
    
    def compress_message(self, message: StructuredMessage) -> str:
        """Compress a message using semantic low-entropy protocol.

        Compression strategy (天枢方案书 §5.3 低熵协议):
          1. **Summary extraction** — retain only the first sentence or 80 chars
             of the summary, stripping filler phrases
          2. **Claim deduplication** — merge claims with identical text,
             keep only highest-confidence evidence references
          3. **Artifact path normalisation** — strip common prefixes,
             keep relative paths only
          4. **Risk deduplication** — merge near-duplicate risk descriptions
          5. **Next-action pruning** — keep only actionable items (skip 'log' type)
          6. **Metadata stripping** — remove all non-essential metadata keys
          7. **Compact JSON serialisation** — no whitespace, short keys

        Args:
            message: The message to compress

        Returns:
            Compressed JSON string representation
        """
        compressed: Dict[str, Any] = {}

        # 1. Essential identity fields (always kept)
        compressed["nid"] = message.node_id  # Shortened key for entropy reduction
        compressed["r"] = message.role
        compressed["s"] = message.status

        # 2. Semantic summary compression
        summary = message.summary or ""
        # Take first sentence (up to period/newline) or first 80 chars
        first_sentence = re.split(r'[.\n]', summary)[0].strip()
        compressed["sum"] = first_sentence[:80] if len(first_sentence) > 80 else first_sentence

        # 3. Claim deduplication and compression
        seen_claim_texts: set = set()
        compact_claims: List[Dict[str, Any]] = []
        for claim in message.claims:
            # Handle both dict and object claims
            if isinstance(claim, dict):
                text = claim.get("text", "").strip()
                evidence = claim.get("evidence", [])
                confidence = claim.get("confidence", 0.5)
            else:
                text = getattr(claim, "text", "").strip()
                evidence = getattr(claim, "evidence", [])
                confidence = getattr(claim, "confidence", 0.5)
            
            if not text or text in seen_claim_texts:
                continue
            seen_claim_texts.add(text)
            # Keep only text + top 2 evidence refs
            compact_claim: Dict[str, Any] = {"t": text}
            if evidence:
                compact_claim["e"] = evidence[:2]
            if confidence != 0.5:  # Only store non-default confidence
                compact_claim["c"] = round(confidence, 2)
            compact_claims.append(compact_claim)
        if compact_claims:
            compressed["cl"] = compact_claims  # Shortened key

        # 4. Artifact path normalisation
        compact_artifacts: List[str] = []
        for artifact in message.artifacts:
            # Strip common prefixes to save tokens
            path = artifact
            for prefix in ("/tmp/", "/home/", "C:\\Users\\", "C:/Users/"):
                if path.startswith(prefix):
                    path = "~/" + path[len(prefix):]
                    break
            compact_artifacts.append(path)
        if compact_artifacts:
            compressed["art"] = compact_artifacts[:5]  # Shortened key

        # 5. Risk deduplication (merge near-duplicates by keyword overlap)
        unique_risks: List[str] = []
        risk_keywords: List[set] = []
        for risk in message.risks:
            words = set(risk.lower().split())
            is_duplicate = any(
                len(words & kw) / max(len(words | kw), 1) > 0.6
                for kw in risk_keywords
            )
            if not is_duplicate:
                unique_risks.append(risk[:60])  # Cap each risk at 60 chars
                risk_keywords.append(words)
        if unique_risks:
            compressed["rk"] = unique_risks[:3]  # Shortened key

        # 6. Next-action pruning — keep only actionable types
        actionable_types = {"spawn", "revise", "retry", "escalate", "recover"}
        compact_actions: List[Dict[str, Any]] = []
        for action in message.next_actions:
            action_type = action.get("type", "")
            if action_type in actionable_types:
                compact_action: Dict[str, Any] = {
                    "t": action_type,  # Shortened key
                    "tgt": action.get("target", ""),  # Shortened key
                }
                reason = action.get("reason", "")
                if reason:
                    compact_action["rsn"] = reason[:40]  # Shortened key
                compact_actions.append(compact_action)
        if compact_actions:
            compressed["na"] = compact_actions[:3]  # Shortened key

        # 7. Metadata — strip entirely (non-essential for downstream routing)
        # Only preserve budget_used if it has meaningful data
        if message.budget_used:
            non_zero_budget = {
                k: v for k, v in message.budget_used.items()
                if v and v != 0 and v != 0.0
            }
            if non_zero_budget:
                # Shorten budget keys
                compact_budget = {}
                if "prompt_tokens" in non_zero_budget:
                    compact_budget["pt"] = non_zero_budget["prompt_tokens"]
                if "completion_tokens" in non_zero_budget:
                    compact_budget["ct"] = non_zero_budget["completion_tokens"]
                if "wall_time_s" in non_zero_budget:
                    compact_budget["wt"] = non_zero_budget["wall_time_s"]
                if compact_budget:
                    compressed["b"] = compact_budget

        return json.dumps(compressed, ensure_ascii=False, separators=(',', ':'))
    
    def calculate_compression_ratio(self, original: StructuredMessage, compressed: str) -> float:
        """Calculate the compression ratio.
        
        Args:
            original: The original message
            compressed: The compressed message string
            
        Returns:
            Compression ratio (original_size / compressed_size)
        """
        original_size = len(self.serialize_message(original))
        compressed_size = len(compressed)
        
        if compressed_size == 0:
            return 0.0
        
        return original_size / compressed_size
    
    def calculate_quality_metrics(
        self, original: StructuredMessage, compressed: str
    ) -> Dict[str, float]:
        """Calculate low-entropy communication quality metrics.
        
        These metrics prove that compression preserves valid evidence and 
        essential information, not just achieves size reduction.
        
        Args:
            original: The original message before compression
            compressed: The compressed message string (JSON)
            
        Returns:
            Dict with quality metrics:
            - payload_efficiency: ratio of essential payload to total size
            - claim_evidence_coverage: fraction of claims with evidence retained
            - key_field_retention: fraction of critical fields preserved
            - artifact_path_retention: fraction of artifacts preserved
            - action_retention: fraction of actionable items preserved
        """
        metrics: Dict[str, float] = {}
        
        # Parse compressed message
        try:
            compressed_data = json.loads(compressed)
        except (json.JSONDecodeError, TypeError):
            return {
                "payload_efficiency": 0.0,
                "claim_evidence_coverage": 0.0,
                "key_field_retention": 0.0,
                "artifact_path_retention": 0.0,
                "action_retention": 0.0,
            }
        
        # 1. Payload efficiency — essential payload (claims + artifacts + actions) vs total
        compressed_size = len(compressed)
        essential_parts = (
            compressed_data.get("cl", []) +  # claims
            compressed_data.get("art", []) +  # artifacts
            compressed_data.get("na", [])  # next actions
        )
        essential_size = len(json.dumps(essential_parts, ensure_ascii=False, separators=(',', ':')))
        metrics["payload_efficiency"] = (
            essential_size / compressed_size if compressed_size > 0 else 0.0
        )
        
        # 2. Claim evidence coverage — fraction of original claims with evidence retained
        original_claims = original.claims
        if original_claims:
            claims_with_evidence = sum(
                1 for claim in original_claims
                if (claim.get("evidence") if isinstance(claim, dict) 
                    else getattr(claim, "evidence", []))
            )
            compressed_claims = compressed_data.get("cl", [])
            retained_claims = len(compressed_claims)
            # Coverage = (retained claims with evidence) / (original claims with evidence)
            retained_with_evidence = sum(
                1 for c in compressed_claims if c.get("e")
            )
            metrics["claim_evidence_coverage"] = (
                retained_with_evidence / claims_with_evidence 
                if claims_with_evidence > 0 else 1.0
            )
        else:
            metrics["claim_evidence_coverage"] = 1.0  # No claims to lose
        
        # 3. Key field retention — critical fields preserved
        critical_fields = ["node_id", "role", "status", "summary"]
        retained_fields = sum(
            1 for field in critical_fields
            if compressed_data.get(self._field_to_short_key(field))
        )
        metrics["key_field_retention"] = retained_fields / len(critical_fields)
        
        # 4. Artifact path retention — fraction of artifacts preserved
        original_artifacts = original.artifacts
        if original_artifacts:
            compressed_artifacts = compressed_data.get("art", [])
            metrics["artifact_path_retention"] = (
                len(compressed_artifacts) / len(original_artifacts)
            )
        else:
            metrics["artifact_path_retention"] = 1.0
        
        # 5. Action retention — actionable items preserved
        original_actions = [
            a for a in original.next_actions
            if a.get("type") in {"spawn", "revise", "retry", "escalate", "recover"}
        ]
        if original_actions:
            compressed_actions = compressed_data.get("na", [])
            metrics["action_retention"] = (
                len(compressed_actions) / len(original_actions)
            )
        else:
            metrics["action_retention"] = 1.0
        
        return metrics
    
    def _field_to_short_key(self, field: str) -> str:
        """Map original field names to their shortened compressed keys."""
        mapping = {
            "node_id": "nid",
            "role": "r",
            "status": "s",
            "summary": "sum",
            "claims": "cl",
            "artifacts": "art",
            "risks": "rk",
            "next_actions": "na",
            "budget_used": "b",
        }
        return mapping.get(field, field)
    
    def extract_key_information(self, message: StructuredMessage) -> Dict[str, Any]:
        """Extract key information from a message for routing and decision making.
        
        Args:
            message: The message to extract information from
            
        Returns:
            Dictionary with key information
        """
        return {
            "node_id": message.node_id,
            "role": message.role,
            "status": message.status,
            "has_artifacts": len(message.artifacts) > 0,
            "has_risks": len(message.risks) > 0,
            "next_action_types": [action["type"] for action in message.next_actions],
            "claim_count": len(message.claims),
            "tokens_used": message.budget_used.get("prompt_tokens", 0) + 
                          message.budget_used.get("completion_tokens", 0),
            "time_used": message.budget_used.get("wall_time_s", 0)
        }
