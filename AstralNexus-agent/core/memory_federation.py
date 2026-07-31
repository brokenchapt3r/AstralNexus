from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import Fact


@dataclass
class MemoryEntry:
    """Represents an entry in the memory system."""
    
    # Unique identifier
    entry_id: str
    
    # Content of the memory entry
    content: str
    
    # Type of memory (short_term, medium_term, long_term)
    memory_type: str
    
    # Source of the memory (node_id, user, system, etc.)
    source: str
    
    # Timestamp when the entry was created
    created_at: str
    
    # Timestamp when the entry was last accessed
    last_accessed: str
    
    # Confidence level (0.0 to 1.0)
    confidence: float = 1.0
    
    # Tags associated with the entry
    tags: List[str] = field(default_factory=list)
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompressionRecall:
    """Information preserved before lossy context compression."""

    node_id: str
    relevant_facts: List[str] = field(default_factory=list)
    captured_constraints: List[str] = field(default_factory=list)
    total_captured: int = 0


class MemoryFederation:
    """Federated memory system for AstralNexus."""
    
    def __init__(self, store=None):
        """Initialize memory federation.
        
        Args:
            store: Optional SQLiteTaskGraphStore for persistence
        """
        self.store = store
        # Memory tiers are bounded runtime views; SQLite remains the durable fact source.
        self.short_term_memory = {}
        self.medium_term_memory = {}
        self.long_term_memory = {}
        
        # Load existing facts from store if available
        if self.store:
            facts = self.store.facts()
            for fact in facts:
                self.add_fact(fact)
    
    def seed_from_task_graph(self, task_graph=None) -> int:
        """Seed goal, constraint and acceptance memory from the canonical graph."""
        nodes = []
        if task_graph is not None:
            nodes = list(task_graph.nodes.values())
        elif self.store is not None:
            try:
                nodes = list(self.store.nodes())
            except Exception:
                nodes = []
        for node in nodes:
            self.sync_task_node(node)
        return len(nodes)

    def sync_task_node(self, node) -> None:
        """Persist the non-negotiable contract of one admitted task node."""
        node_id = node.get("node_id") if isinstance(node, dict) else node.node_id
        goal = node.get("goal", "") if isinstance(node, dict) else node.goal
        constraints = (
            node.get("constraints", []) if isinstance(node, dict) else node.constraints
        ) or []
        acceptance = (
            node.get("acceptance_criteria", [])
            if isinstance(node, dict)
            else node.acceptance_criteria
        ) or []
        if goal:
            recorded_at = datetime.now(timezone.utc).isoformat()
            self.add_fact(Fact(
                fact_id=f"fact_{node_id}_goal",
                text=f"Node {node_id} goal: {goal}",
                source=node_id,
                confidence=1.0,
                recorded_at=recorded_at,
                metadata={"type": "goal", "node_id": node_id},
            ))
        self.add_acceptance_criteria(node_id, list(acceptance))
        for index, constraint in enumerate(constraints):
            entry_id = f"constraint_{node_id}_{index}"
            now = datetime.now(timezone.utc).isoformat()
            self.add_memory_entry(MemoryEntry(
                entry_id=entry_id,
                content=str(constraint),
                memory_type="medium_term",
                source=node_id,
                created_at=now,
                last_accessed=now,
                confidence=1.0,
                tags=["constraint", f"node:{node_id}"],
                metadata={"type": "constraint", "node_ids": [node_id]},
            ))
    
    def add_fact(self, fact):
        """Add a fact to the memory system.
        
        Args:
            fact: Fact object to add
        """
        # Add to medium-term memory by default
        self.medium_term_memory[fact.fact_id] = fact
        
        # Persist to store if available
        if self.store:
            try:
                self.store.add_fact(fact)
            except Exception:
                # Store might not support add_fact yet, ignore
                pass
    
    def get_all_facts(self):
        """Get all facts from all memory layers.
        
        Returns:
            List of all Fact objects
        """
        return (list(self.short_term_memory.values()) + 
                list(self.medium_term_memory.values()) + 
                list(self.long_term_memory.values()))
    
    def add_constraint_change(self, constraint_text, source=None, affected_nodes=None, **kwargs):
        """Add a constraint change with versioning.
        
        Args:
            constraint_text: The constraint text
            source: Optional source identifier
            affected_nodes: Optional list of affected node IDs
            
        Returns:
            Constraint ID
        """
        import time
        from datetime import datetime, timezone

        source_node_id = kwargs.get("source_node_id")
        if source_node_id:
            source = source or source_node_id
            if affected_nodes is None:
                affected_nodes = [source_node_id]
        
        constraint_id = f"constraint_{int(time.time())}_{len(self.medium_term_memory)}"
        recorded_at = datetime.now(timezone.utc).isoformat()
        
        # Check if there's a previous version to supersede
        previous_version = None
        version = 1
        if self.store:
            try:
                versions = self.store.constraint_versions()
                if versions:
                    previous_version = versions[-1]["constraint_id"]
                    version = versions[-1]["version"] + 1
            except AttributeError:
                # Store doesn't have constraint_versions method yet
                pass
            except Exception:
                # Other errors, just use default version
                pass
        
        # Create fact for the constraint
        fact = Fact(
            fact_id=f"fact_{constraint_id}",
            text=constraint_text,
            source=source or "constraint",
            confidence=1.0,
            recorded_at=recorded_at,
            metadata={
                "type": "constraint",
                "constraint_id": constraint_id,
                "affected_nodes": affected_nodes or [],
                "supersedes": previous_version,
                "version": version
            }
        )
        
        self.add_fact(fact)
        
        # Create a memory entry for the constraint
        tags = ["constraint", "constraint_change"]
        if affected_nodes:
            tags.extend([f"node:{node_id}" for node_id in affected_nodes])
        
        entry = MemoryEntry(
            entry_id=constraint_id,
            content=constraint_text,
            memory_type="medium_term",
            source=source or "constraint",
            created_at=recorded_at,
            last_accessed=recorded_at,
            confidence=1.0,
            tags=tags,
            metadata={
                "type": "constraint",
                "constraint_id": constraint_id,
                "affected_nodes": affected_nodes or [],
                "supersedes": previous_version,
                "version": version,
                "source": source or "constraint"
            }
        )
        
        self.add_memory_entry(entry)
        
        # Store the constraint in the task graph store if available
        if self.store:
            try:
                self.store.add_constraint_version({
                    "constraint_id": constraint_id,
                    "text": constraint_text,
                    "source": source,
                    "affected_nodes": affected_nodes or [],
                    "supersedes": previous_version,
                    "version": version
                })
            except Exception:
                # Store might not support constraint versioning yet, ignore
                pass
        
        return constraint_id

    def add_acceptance_criteria(self, node_id: str, criteria: List[str]) -> None:
        """Store acceptance criteria as recallable memory entries."""
        recorded_at = datetime.now(timezone.utc).isoformat()
        for index, criterion in enumerate(criteria):
            entry_id = f"acceptance_{node_id}_{index}"
            entry = MemoryEntry(
                entry_id=entry_id,
                content=criterion,
                memory_type="medium_term",
                source=node_id,
                created_at=recorded_at,
                last_accessed=recorded_at,
                confidence=1.0,
                tags=["acceptance", f"node:{node_id}"],
                metadata={
                    "type": "acceptance_criteria",
                    "node_ids": [node_id],
                    "source": node_id,
                },
            )
            self.add_memory_entry(entry)
    
    def add_fact_to_memory(self, fact: Fact) -> None:
        """Add a fact to the medium-term memory.
        
        Args:
            fact: The fact to add to memory
        """
        entry = MemoryEntry(
            entry_id=fact.fact_id,
            content=fact.text,
            memory_type="medium_term",
            source=fact.source,
            created_at=fact.recorded_at,
            last_accessed=fact.recorded_at,
            confidence=fact.confidence,
            tags=["fact"],
            metadata=fact.metadata
        )
        
        self.medium_term_memory[fact.fact_id] = entry
    
    def add_memory_entry(self, entry: MemoryEntry) -> None:
        """Add a memory entry to the appropriate memory layer.
        
        Args:
            entry: The memory entry to add
        """
        if entry.memory_type == "short_term":
            self.short_term_memory[entry.entry_id] = entry
        elif entry.memory_type == "medium_term":
            self.medium_term_memory[entry.entry_id] = entry
        elif entry.memory_type == "long_term":
            self.long_term_memory[entry.entry_id] = entry
    
    def get_memory_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        """Get a memory entry by its ID, searching across all memory layers.
        
        Args:
            entry_id: The ID of the entry to retrieve
            
        Returns:
            The memory entry if found, None otherwise
        """
        # Search in order of specificity: short-term, medium-term, long-term
        if entry_id in self.short_term_memory:
            entry = self.short_term_memory[entry_id]
            entry.last_accessed = datetime.now(timezone.utc).isoformat()
            return entry
        
        if entry_id in self.medium_term_memory:
            entry = self.medium_term_memory[entry_id]
            entry.last_accessed = datetime.now(timezone.utc).isoformat()
            return entry
        
        if entry_id in self.long_term_memory:
            entry = self.long_term_memory[entry_id]
            entry.last_accessed = datetime.now(timezone.utc).isoformat()
            return entry
        
        return None
    
    def search_memory(self, query: str, memory_types: List[str] = None, top_k: int = 5) -> List[MemoryEntry]:
        """Search memory for entries matching a query.
        
        Args:
            query: The search query
            memory_types: List of memory types to search in (default: all)
            top_k: Maximum number of results to return
            
        Returns:
            List of matching memory entries
        """
        if memory_types is None:
            memory_types = ["short_term", "medium_term", "long_term"]
        
        # Collect entries from specified memory layers
        entries = []
        if "short_term" in memory_types:
            entries.extend(self.short_term_memory.values())
        if "medium_term" in memory_types:
            entries.extend(self.medium_term_memory.values())
        if "long_term" in memory_types:
            entries.extend(self.long_term_memory.values())
        
        # Simple keyword matching - in a real implementation, this might use embeddings
        query_lower = query.lower()
        matching_entries = []
        
        for entry in entries:
            # Handle both MemoryEntry and Fact objects
            if hasattr(entry, 'content'):
                # MemoryEntry object
                text = entry.content
                tags = entry.tags
            elif hasattr(entry, 'text'):
                # Fact object
                text = entry.text
                tags = [entry.metadata.get('type', '')] if hasattr(entry, 'metadata') else []
            else:
                continue
            
            # Split query into words for better matching
            query_words = query_lower.split()
            
            # Check if any query word appears in text or tags
            text_match = any(word in text.lower() for word in query_words)
            tag_match = any(any(word in tag.lower() for word in query_words) for tag in tags if tag)
            
            if text_match or tag_match:
                matching_entries.append(entry)
        
        # Sort by confidence and recency
        def sort_key(x):
            conf = x.confidence if hasattr(x, 'confidence') else 1.0
            # Handle both MemoryEntry (last_accessed) and Fact (recorded_at)
            timestamp = getattr(x, 'last_accessed', None) or getattr(x, 'recorded_at', '')
            return (conf, timestamp)
        
        matching_entries.sort(key=sort_key, reverse=True)
        
        # Return top_k results
        return matching_entries[:top_k]
    
    def recall_context(self, node_id: str, query: str = "", budget: int = 1000):
        """Recall context for a specific node.
        
        Implements a multi-strategy recall approach that scales to 100+ nodes:
        1. Direct fact access for large graphs (bypasses brittle keyword matching)
        2. Keyword-based search for smaller graphs
        3. Budget-aware filtering to prevent context overflow
        
        Args:
            node_id: The ID of the node to recall context for
            query: Optional query to focus the recall
            budget: Character budget for the recalled context
            
        Returns:
            ContextRecall object with structured context information
        """
        from dataclasses import dataclass, field
        from typing import List
        
        @dataclass
        class ContextRecall:
            """Structured context recall result."""
            active_constraints: List[str] = field(default_factory=list)
            relevant_facts: List[str] = field(default_factory=list)
            decisions: List[str] = field(default_factory=list)
            artifacts: List[str] = field(default_factory=list)
            failures: List[str] = field(default_factory=list)
            acceptance_criteria: List[str] = field(default_factory=list)
            tokens_estimate: int = 0
            budget: int = 0
            
            def __str__(self):
                parts = []
                for constraint in self.active_constraints:
                    parts.append(f"Constraint: {constraint}")
                for fact in self.relevant_facts:
                    parts.append(f"Fact: {fact}")
                for decision in self.decisions:
                    parts.append(f"Decision: {decision}")
                for artifact in self.artifacts:
                    parts.append(f"Artifact: {artifact}")
                for failure in self.failures:
                    parts.append(f"Failure: {failure}")
                for criterion in self.acceptance_criteria:
                    parts.append(f"Acceptance: {criterion}")
                return "\n".join(parts)
        
        result = ContextRecall(budget=budget)
        used_chars = 0
        
        # Helper function to get text from entry
        def get_text(entry):
            return entry.content if hasattr(entry, 'content') else entry.text
        
        # Count total facts to determine recall strategy
        total_facts = len(self.medium_term_memory) + len(self.short_term_memory) + len(self.long_term_memory)
        
        # 1. Active constraints - use multi-strategy approach for scale
        # First try keyword search with higher top_k for large graphs
        constraint_top_k = max(5, total_facts // 5) if total_facts > 20 else 5
        constraints = self.search_memory(f"constraint", ["medium_term", "long_term"], top_k=constraint_top_k)
        
        # Filter to only include constraints that affect this node
        filtered_constraints = [
            c for c in constraints 
            if (hasattr(c, 'metadata') and node_id in c.metadata.get('affected_nodes', []))
            or (hasattr(c, 'tags') and any(f"node:{node_id}" in tag for tag in c.tags))
        ]
        
        # Fallback: if no constraints matched via filtering, directly scan all memory
        # for constraint entries that reference this node
        if not filtered_constraints and total_facts > 20:
            all_entries = list(self.medium_term_memory.values()) + list(self.long_term_memory.values())
            for entry in all_entries:
                # Check if this is a constraint entry
                entry_tags = getattr(entry, 'tags', [])
                entry_meta = getattr(entry, 'metadata', {})
                is_constraint = (
                    'constraint' in entry_tags 
                    or entry_meta.get('type') == 'constraint'
                    or 'constraint' in getattr(entry, 'content', '').lower()[:50]
                )
                if is_constraint:
                    # Check if it affects this node
                    affects_node = (
                        node_id in entry_meta.get('affected_nodes', [])
                        or any(f"node:{node_id}" in tag for tag in entry_tags)
                    )
                    if affects_node:
                        filtered_constraints.append(entry)
                        if len(filtered_constraints) >= 10:
                            break

        # Large graph fallback: keep global constraints alive even when the
        # caller did not bind them to an affected node. This is important for
        # long-horizon compression wakeup tests where constraints are global.
        if not filtered_constraints and total_facts > 20:
            all_entries = list(self.medium_term_memory.values()) + list(self.long_term_memory.values())
            for entry in all_entries:
                entry_tags = getattr(entry, 'tags', [])
                entry_meta = getattr(entry, 'metadata', {})
                entry_text = get_text(entry).lower()
                is_constraint = (
                    'constraint' in entry_tags
                    or entry_meta.get('type') == 'constraint'
                    or 'constraint' in entry_text[:80]
                    or '约束' in entry_text[:80]
                )
                if is_constraint:
                    filtered_constraints.append(entry)
                    if len(filtered_constraints) >= 10:
                        break
        
        for constraint in filtered_constraints:
            text = get_text(constraint)
            if used_chars + len(text) <= budget:
                result.active_constraints.append(text)
                used_chars += len(text) + 1  # +1 for newline
        
        # 2. Dependency decisions
        decisions = self.search_memory(f"decision:{node_id}", ["medium_term"])
        for decision in decisions:
            text = get_text(decision)
            if used_chars + len(text) <= budget:
                result.decisions.append(text)
                used_chars += len(text) + 1
        
        # 3. Relevant facts - use direct access for large graphs (100+ nodes)
        if total_facts > 50:
            # Large graph: directly access all facts from memory layers
            # This bypasses brittle keyword matching that fails at scale
            all_facts = self.get_all_facts()
            # Sort by confidence (highest first) and take top 20
            sorted_facts = sorted(all_facts, key=lambda f: getattr(f, 'confidence', 0.5), reverse=True)
            facts_to_add = sorted_facts[:20]
        else:
            # Small graph: use keyword-based search
            facts_to_add = self.search_memory(query or "fact", ["medium_term"], top_k=8)
        
        for fact in facts_to_add:
            text = get_text(fact)
            if used_chars + len(text) <= budget:
                result.relevant_facts.append(text)
                used_chars += len(text) + 1
        
        # 4. Artifact summaries
        artifacts = self.search_memory(f"artifact:{node_id}", ["medium_term"], top_k=5)
        for artifact in artifacts:
            text = get_text(artifact)
            if used_chars + len(text) <= budget:
                result.artifacts.append(text)
                used_chars += len(text) + 1
        
        # 5. Recent failures
        failures = self.search_memory("failure", ["short_term", "medium_term"], top_k=3)
        for failure in failures:
            text = get_text(failure)
            if used_chars + len(text) <= budget:
                result.failures.append(text)
                used_chars += len(text) + 1
        
        # 6. Acceptance criteria - use multi-strategy approach for scale
        # First try specific node search with higher top_k
        criteria_top_k = max(5, total_facts // 5) if total_facts > 20 else 5
        criteria = self.search_memory(f"acceptance:{node_id}", ["medium_term"], top_k=criteria_top_k)
        
        # Filter to only include acceptance criteria for this node
        filtered_criteria = [
            c for c in criteria
            if (hasattr(c, 'metadata') and node_id in c.metadata.get('node_ids', []))
            or (hasattr(c, 'tags') and any(f"node:{node_id}" in tag for tag in c.tags))
            or (node_id in get_text(c))
        ]
        
        # Fallback: if no criteria matched, directly scan all memory
        if not filtered_criteria and total_facts > 20:
            all_entries = list(self.medium_term_memory.values()) + list(self.long_term_memory.values())
            for entry in all_entries:
                entry_tags = getattr(entry, 'tags', [])
                entry_meta = getattr(entry, 'metadata', {})
                entry_text = get_text(entry).lower()
                
                is_acceptance = (
                    'acceptance' in entry_tags
                    or entry_meta.get('type') == 'acceptance_criteria'
                    or 'acceptance' in entry_text[:50]
                    or '验收' in entry_text[:50]
                    or '验收标准' in entry_text[:50]
                )
                
                if is_acceptance:
                    affects_node = (
                        node_id in entry_meta.get('node_ids', [])
                        or any(f"node:{node_id}" in tag for tag in entry_tags)
                        or node_id in get_text(entry)
                    )
                    if affects_node:
                        filtered_criteria.append(entry)
                        if len(filtered_criteria) >= 10:
                            break

        # Large graph fallback: if node-specific acceptance criteria were not
        # seeded, surface global acceptance memories instead of returning empty.
        if not filtered_criteria and total_facts > 20:
            all_entries = list(self.medium_term_memory.values()) + list(self.long_term_memory.values())
            for entry in all_entries:
                entry_tags = getattr(entry, 'tags', [])
                entry_meta = getattr(entry, 'metadata', {})
                entry_text = get_text(entry).lower()
                is_acceptance = (
                    'acceptance' in entry_tags
                    or entry_meta.get('type') == 'acceptance_criteria'
                    or 'acceptance' in entry_text[:80]
                    or '验收' in entry_text[:80]
                )
                if is_acceptance:
                    filtered_criteria.append(entry)
                    if len(filtered_criteria) >= 10:
                        break
        
        for criterion in filtered_criteria:
            text = get_text(criterion)
            if used_chars + len(text) <= budget:
                result.acceptance_criteria.append(text)
                used_chars += len(text) + 1
        
        result.tokens_estimate = used_chars
        return result
    
    def compress_memory(self, threshold_age_hours: float = 24.0) -> int:
        """Compress memory by moving older entries between layers.
        
        Args:
            threshold_age_hours: Age threshold for moving entries to longer-term storage
            
        Returns:
            Number of entries moved
        """
        moved_count = 0
        current_time = datetime.now(timezone.utc)
        
        # Move old short-term entries to medium-term
        to_move = []
        for entry_id, entry in self.short_term_memory.items():
            timestamp = getattr(entry, "created_at", None) or getattr(entry, "recorded_at", None)
            entry_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else current_time
            age_hours = (current_time - entry_time).total_seconds() / 3600
            
            if age_hours >= threshold_age_hours:
                to_move.append(entry_id)
        
        for entry_id in to_move:
            entry = self.short_term_memory.pop(entry_id)
            entry.memory_type = "medium_term"
            self.medium_term_memory[entry_id] = entry
            moved_count += 1
        
        # Move old medium-term entries to long-term
        to_move = []
        for entry_id, entry in self.medium_term_memory.items():
            timestamp = getattr(entry, "created_at", None) or getattr(entry, "recorded_at", None)
            entry_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else current_time
            age_hours = (current_time - entry_time).total_seconds() / 3600
            
            if age_hours >= threshold_age_hours * 7:  # 7 times longer for long-term
                to_move.append(entry_id)
        
        for entry_id in to_move:
            entry = self.medium_term_memory.pop(entry_id)
            entry.memory_type = "long_term"
            self.long_term_memory[entry_id] = entry
            moved_count += 1
        
        return moved_count
    
    def prune_memory(self, max_short_term: int = 100, max_medium_term: int = 1000) -> int:
        """Prune memory by removing old or low-confidence entries.
        
        Args:
            max_short_term: Maximum number of short-term entries to keep
            max_medium_term: Maximum number of medium-term entries to keep
            
        Returns:
            Number of entries pruned
        """
        pruned_count = 0
        
        # Prune short-term memory if over limit
        if len(self.short_term_memory) > max_short_term:
            # Sort by last accessed time and confidence
            sorted_entries = sorted(
                self.short_term_memory.items(),
                key=lambda x: (datetime.fromisoformat(x[1].last_accessed.replace("Z", "+00:00")), x[1].confidence)
            )
            
            # Remove oldest entries
            to_remove = len(self.short_term_memory) - max_short_term
            for i in range(to_remove):
                entry_id = sorted_entries[i][0]
                del self.short_term_memory[entry_id]
                pruned_count += 1
        
        # Prune medium-term memory if over limit
        if len(self.medium_term_memory) > max_medium_term:
            # Sort by last accessed time and confidence
            sorted_entries = sorted(
                self.medium_term_memory.items(),
                key=lambda x: (datetime.fromisoformat(x[1].last_accessed.replace("Z", "+00:00")), x[1].confidence)
            )
            
            # Remove oldest entries
            to_remove = len(self.medium_term_memory) - max_medium_term
            for i in range(to_remove):
                entry_id = sorted_entries[i][0]
                del self.medium_term_memory[entry_id]
                pruned_count += 1
        
        return pruned_count
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about the memory system.
        
        Returns:
            Dictionary with memory statistics
        """
        return {
            "short_term_memory_count": len(self.short_term_memory),
            "medium_term_memory_count": len(self.medium_term_memory),
            "long_term_memory_count": len(self.long_term_memory),
            "total_memory_count": len(self.short_term_memory) + len(self.medium_term_memory) + len(self.long_term_memory)
        }
    
    def capture_before_compression(self, node_id: str, transcript: str) -> CompressionRecall:
        """Capture important information from transcript before compression.
        
        This method extracts key facts and constraints from a transcript
        to ensure they are preserved even if the full transcript is compressed.
        
        Args:
            node_id: The ID of the node processing the transcript
            transcript: The transcript text to analyze
            
        Returns:
            Dictionary containing extracted facts and constraints
        """
        import re
        
        captured_facts = []
        captured_constraints = []
        recorded_at = datetime.now(timezone.utc).isoformat()
        
        # Extract potential facts (sentences with specific keywords)
        fact_keywords = [
            "must", "require", "need", "should", "will", "shall",
            "标准", "规范", "要求", "必须", "应该", "需要", "验收", "早期", "兼容", "接口"
        ]
        sentences = re.split(r'[.!?]+', transcript)
        
        for i, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # Check if sentence contains fact keywords
            if any(keyword in sentence.lower() for keyword in fact_keywords):
                fact_id = f"fact_captured_{node_id}_{len(captured_facts)}"
                fact = Fact(
                    fact_id=fact_id,
                    text=sentence,
                    source=f"transcript:{node_id}",
                    confidence=0.9,
                    recorded_at=recorded_at,
                    metadata={
                        "type": "captured_fact",
                        "node_id": node_id,
                        "sentence_index": i
                    }
                )
                self.add_fact(fact)
                captured_facts.append(sentence)
        
        # Keywords that indicate constraints
        constraint_keywords = [
            "constraint", "limit", "restrict", "rule", "policy",
            "约束", "限制", "规则", "政策", "不得", "不能", "禁止"
        ]
        for i, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            
            if any(keyword in sentence.lower() for keyword in constraint_keywords):
                constraint_id = f"constraint_captured_{node_id}_{len(captured_constraints)}"
                
                # Create a fact for the constraint as well
                fact = Fact(
                    fact_id=f"fact_{constraint_id}",
                    text=sentence,
                    source=f"transcript:{node_id}",
                    confidence=0.85,
                    recorded_at=recorded_at,
                    metadata={
                        "type": "captured_constraint",
                        "constraint_id": constraint_id,
                        "node_id": node_id,
                        "sentence_index": i
                    }
                )
                self.add_fact(fact)
                captured_constraints.append(sentence)
        
        return CompressionRecall(
            node_id=node_id,
            relevant_facts=captured_facts,
            captured_constraints=captured_constraints,
            total_captured=len(captured_facts) + len(captured_constraints)
        )
