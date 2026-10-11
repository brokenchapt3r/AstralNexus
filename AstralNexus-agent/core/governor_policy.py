"""Configurable policy thresholds enforced by the Global Governor."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GovernorPolicy:
    admission_threshold: float = 0.65
    max_active_tasks: int = 2000
    max_total_tasks: int = 10000
    interactive_context_chars: int = 4000
    interactive_response_tokens: int = 1200
    interactive_complexity_threshold: float = 0.70
    interactive_parallel_threshold: float = 0.50
    interactive_artifact_threshold: float = 0.45
    interactive_min_graph_nodes: int = 3
    interactive_max_estimated_nodes: int = 10000
    duplicate_similarity_threshold: float = 0.88
    false_parallel_min_nodes: int = 4
    false_parallel_min_frontier: int = 2
    false_parallel_wide_capabilities: int = 3
    false_parallel_wide_outputs: int = 3
    false_parallel_wide_acceptance: int = 3
    allow_safe_cycle_repair: bool = True
    max_cycle_repairs_per_patch: int = 8
    initial_contract_min_output_criterion_coverage: float = 1.0
    initial_contract_min_total_criterion_coverage: float = 1.0
