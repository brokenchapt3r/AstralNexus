"""Heterogeneous Worker Registry: truly heterogeneous agents.

Key principle: Workers are NOT the same model with different prompts.
They differ in:
- Tools available (each worker has unique tool sets)
- Model preferences (different models for different tasks)
- Resource tier (local/edge/cloud based on task needs)
- Capability contracts (formal input/output specifications)
- Cost profiles (different token/latency budgets)

The 9 worker types from the advanced spec:
1. Retrieval Worker - search, knowledge base, document retrieval
2. Reasoning Worker - analysis, reasoning, scheme comparison
3. Coding Worker - code generation, modification, debugging
4. Tool Worker - Python, SQL, Shell, API execution
5. Vision Worker - image, UI, video, document visual analysis
6. Device Worker - terminal control, automation
7. Critic Worker - counter-example, vulnerability, risk check
8. Verifier Worker - acceptance, testing, evidence verification
9. Recovery Worker - node failure, exception recovery, plan repair
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WorkerProfile:
    """Profile for a heterogeneous worker type.
    
    Each profile defines:
    - Unique tool set (not shared across workers)
    - Model preferences (which model tier to use)
    - Resource tier preference (local/edge/cloud)
    - Input/output contracts
    - Cost and latency budgets
    """
    worker_type: str
    description: str
    
    # Unique tools for this worker
    tools: List[str]
    
    # Model preferences: which model tier/size to use
    model_preference: str  # "small_fast", "medium_balanced", "large_reasoning", "vision_specialized"
    
    # Resource tier preference
    resource_tier: str  # "local", "edge", "cloud"
    
    # Formal contracts
    input_contract: str
    output_contract: str
    
    # Capabilities (formal skill list)
    capabilities: List[str]

    # Reasoning is an execution attribute, not a matchable business
    # capability.  Keeping it separate prevents generic words such as
    # "analysis" from routing architecture work to an unrelated Worker.
    reasoning_level: str = "medium"
    
    # Cost profile
    avg_token_budget: int = 4000
    max_latency_seconds: float = 30.0
    cost_per_call: str = "low"  # low/medium/high
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


# The 9 heterogeneous worker profiles
HETEROGENEOUS_WORKERS: Dict[str, WorkerProfile] = {
    "Retrieval": WorkerProfile(
        worker_type="Retrieval",
        description="Web, knowledge base, codebase, document retrieval",
        tools=[
            "web_search",
            "http_get",
            "openalex_search",
            "crossref_search",
            "arxiv_search",
            "github_repo_contents",
            "file_read",
            "browser_fetch",
        ],
        model_preference="small_fast",  # Retrieval doesn't need heavy reasoning
        resource_tier="edge",  # Needs network access
        input_contract="query: str, sources: List[str], constraints: List[str]",
        output_contract="results: List[SearchResult], evidence_refs: List[str], confidence: float",
        capabilities=["web_search", "document_retrieval", "database_query", "knowledge_base"],
        avg_token_budget=2000,
        max_latency_seconds=15.0,
        cost_per_call="low",
    ),
    
    "Reasoning": WorkerProfile(
        worker_type="Reasoning",
        description="Analysis, reasoning, scheme comparison, logical deduction",
        tools=["file_read", "search_files", "python_exec"],
        model_preference="large_reasoning",  # Needs strong reasoning model
        resource_tier="cloud",  # Heavy computation
        input_contract="problem: str, context: Dict, constraints: List[str]",
        output_contract="analysis: AnalysisResult, conclusions: List[str], confidence: float",
        capabilities=["comparison", "scheme_evaluation", "constraint_reasoning", "causal_inference"],
        reasoning_level="high",
        avg_token_budget=8000,
        max_latency_seconds=60.0,
        cost_per_call="high",
    ),
    
    "Architect": WorkerProfile(
        worker_type="Architect",
        description="System architecture, requirements, API, data model, state machine and permission design",
        tools=[
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
            "skills_list",
            "skill_view",
        ],
        model_preference="architect_reasoning",
        resource_tier="cloud",
        input_contract="goal: str, requirements: List[str], constraints: List[str], existing_artifacts: List[str]",
        output_contract="architecture_artifacts: List[str], decisions: List[str], interfaces: List[str], risks: List[str]",
        capabilities=[
            "system_architecture",
            "requirement_analysis",
            "api_design",
            "database_modeling",
            "database_design",
            "state_machine_design",
            "permission_design",
        ],
        reasoning_level="high",
        avg_token_budget=8000,
        max_latency_seconds=120.0,
        cost_per_call="high",
    ),

    "Coding": WorkerProfile(
        worker_type="Coding",
        description="Code generation, modification, debugging, refactoring",
        # These names are the canonical AstralNexus registry names.  A
        # profile must never advertise a conceptual tool that cannot be
        # resolved by ToolRegistry, otherwise the audit says "Coding" while
        # the real worker receives an empty tool list.
        tools=[
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
            "pytest_run",
            "github_repo_contents",
            "skills_list",
            "skill_view",
        ],
        model_preference="coding_specialized",
        resource_tier="cloud",
        input_contract="spec: str, codebase_context: Dict, constraints: List[str]",
        output_contract="code: str, tests: List[str], diff: str, explanation: str",
        capabilities=[
            "code_generation", "programming", "code_modification",
            "debugging", "refactoring", "test_writing",
            "static_analysis", "test_orchestration", "dependency_analysis",
            "api_integration", "api_design", "backend_development",
            "frontend_development", "database_design", "web_app_development",
            "software_implementation", "implementation", "full_stack_development",
            "ui_development", "sql_design", "database_migration",
            "deployment", "containerization", "build_automation",
            "dependency_management",
        ],
        reasoning_level="high",
        avg_token_budget=6000,
        max_latency_seconds=120.0,
        cost_per_call="high",
    ),
    
    "Tool": WorkerProfile(
        worker_type="Tool",
        description="Python, SQL, Shell, API execution - the execution layer",
        tools=["python_exec", "terminal_exec", "http_get", "file_read", "file_write"],
        model_preference="small_fast",  # Tool execution is deterministic
        resource_tier="local",  # Execute locally for speed
        input_contract="command: str, language: str, timeout: int, sandbox: bool",
        output_contract="stdout: str, stderr: str, exit_code: int, artifacts: List[str]",
        capabilities=["python_execution", "shell_commands", "api_calls", "sql_queries"],
        avg_token_budget=1000,
        max_latency_seconds=30.0,
        cost_per_call="low",
    ),
    
    "Vision": WorkerProfile(
        worker_type="Vision",
        description="Image, UI, video, document visual analysis",
        tools=["file_read", "browser_fetch", "python_exec"],
        model_preference="vision_specialized",  # Needs multimodal model
        resource_tier="cloud",  # Vision models are large
        input_contract="image_url: str, analysis_type: str, context: Dict",
        output_contract="description: str, elements: List[VisualElement], confidence: float",
        capabilities=["image_analysis", "ui_inspection", "document_ocr", "video_parse"],
        avg_token_budget=4000,
        max_latency_seconds=45.0,
        cost_per_call="medium",
    ),
    
    "Device": WorkerProfile(
        worker_type="Device",
        description="Terminal control, automation, device operation",
        tools=["terminal_exec", "browser_fetch", "file_read", "file_write"],
        model_preference="small_fast",  # Device control is action-based
        resource_tier="local",  # Must run on local device
        input_contract="action: str, target: str, parameters: Dict",
        output_contract="result: str, screenshot: Optional[str], status: str",
        capabilities=["terminal_control", "automation", "device_operation", "browser_control"],
        avg_token_budget=1500,
        max_latency_seconds=30.0,
        cost_per_call="low",
    ),
    
    "Critic": WorkerProfile(
        worker_type="Critic",
        description="Counter-example generation, vulnerability detection, risk assessment",
        tools=["file_read", "search_files", "python_exec", "pytest_run"],
        model_preference="large_reasoning",  # Critical thinking needs strong model
        resource_tier="cloud",
        input_contract="proposal: str, constraints: List[str], context: Dict",
        output_contract="counter_examples: List[str], risks: List[Risk], severity: str",
        capabilities=["counter_example", "risk_identification", "vulnerability_scan", "edge_case"],
        avg_token_budget=5000,
        max_latency_seconds=45.0,
        cost_per_call="medium",
    ),
    
    "Verifier": WorkerProfile(
        worker_type="Verifier",
        description="Acceptance testing, evidence verification, validation",
        tools=["file_read", "search_files", "python_exec", "pytest_run", "terminal_exec"],
        model_preference="verifier_reasoning",
        resource_tier="edge",
        input_contract="artifact: str, acceptance_criteria: List[str], evidence: List[str]",
        output_contract="passed: bool, coverage: float, failures: List[str], evidence_verified: List[str]",
        capabilities=["evidence_check", "validation", "acceptance_test", "quality_gate"],
        reasoning_level="high",
        avg_token_budget=4000,
        max_latency_seconds=60.0,
        cost_per_call="medium",
    ),

    "Testing": WorkerProfile(
        worker_type="Testing",
        description="Executable unit, integration, end-to-end and regression testing",
        tools=["file_read", "search_files", "terminal_exec", "python_exec", "pytest_run", "http_get"],
        model_preference="testing_balanced",
        resource_tier="edge",
        input_contract="implementation_artifacts: List[str], test_contract: Dict, acceptance_criteria: List[str]",
        output_contract="test_files: List[str], machine_readable_results: List[str], failures: List[str], runtime_evidence: List[str]",
        capabilities=[
            "test_generation",
            "test_execution",
            "unit_testing",
            "integration_testing",
            "end_to_end_testing",
            "api_testing",
            "regression_testing",
        ],
        reasoning_level="medium",
        avg_token_budget=5000,
        max_latency_seconds=120.0,
        cost_per_call="medium",
    ),

    "Synthesizer": WorkerProfile(
        worker_type="Synthesizer",
        description="Final artifact integration, manifest construction and delivery package assembly",
        tools=[
            "file_read",
            "file_write",
            "search_files",
            "terminal_exec",
            "python_exec",
            "pytest_run",
        ],
        model_preference="synthesis_reasoning",
        resource_tier="cloud",
        input_contract="delivery_contract: Dict, verified_artifacts: List[str], verifier_report: Dict, unresolved_gaps: List[str]",
        output_contract="delivery_manifest: str, integrated_artifacts: List[str], completion_summary: str, unresolved_items: List[str]",
        capabilities=[
            "artifact_integration",
            "delivery_packaging",
            "delivery_manifest_generation",
            "cross_artifact_consistency",
            "final_synthesis",
        ],
        reasoning_level="high",
        avg_token_budget=8000,
        max_latency_seconds=120.0,
        cost_per_call="high",
    ),
    
    "Recovery": WorkerProfile(
        worker_type="Recovery",
        description="Node failure recovery, exception handling, plan repair",
        tools=["file_read", "file_write", "search_files", "terminal_exec", "python_exec", "pytest_run"],
        model_preference="medium_balanced",  # Recovery needs moderate reasoning
        resource_tier="edge",
        input_contract="failed_node: str, error: str, graph_state: Dict, checkpoint: Optional[str]",
        output_contract="recovery_plan: List[str], new_nodes: List[str], restored_state: Dict",
        capabilities=["error_recovery", "plan_repair", "exception_handling", "state_rollback"],
        avg_token_budget=3000,
        max_latency_seconds=30.0,
        cost_per_call="medium",
    ),
    
    "Research": WorkerProfile(
        worker_type="Research",
        description="Research tasks: literature review, trend analysis, knowledge synthesis",
        tools=[
            "web_search",
            "http_get",
            "openalex_search",
            "crossref_search",
            "arxiv_search",
            "github_repo_contents",
            "file_read",
            "browser_fetch",
        ],
        model_preference="large_reasoning",  # Research needs strong reasoning
        resource_tier="cloud",
        input_contract="research_question: str, domain: str, constraints: List[str], prior_evidence: List[str]",
        output_contract="findings: str, evidence_refs: List[str], confidence: float, limitations: List[str]",
        capabilities=[
            "knowledge_generation", "literature_synthesis", "academic_writing",
            "trend_analysis", "cross_domain_research", "research_synthesis",
            "evidence_reasoning", "web_search", "knowledge_base",
            # Domain-neutral research planning capabilities.  These are
            # capability-contract tokens, not a task-specific workflow: they
            # cover selection, feasibility, experiment design, and evaluation
            # for any research or planning objective.
            "ai_research_direction_selection", "feasibility_analysis",
            "strategic_evaluation", "research_design",
            "experimental_methodology", "evaluation_metrics_design",
            "experimental_design", "benchmark_design", "evaluation_design",
        ],
        reasoning_level="high",
        avg_token_budget=8000,
        max_latency_seconds=120.0,
        cost_per_call="high",
        metadata={"llm_is_one_of_research_tools": True},
    ),
}


def get_worker_profile(worker_type: str) -> WorkerProfile:
    """Get worker profile by type."""
    if worker_type not in HETEROGENEOUS_WORKERS:
        raise ValueError(f"Unknown worker type: {worker_type}. Available: {list(HETEROGENEOUS_WORKERS.keys())}")
    return HETEROGENEOUS_WORKERS[worker_type]


def get_all_worker_types() -> List[str]:
    """Get all available worker types."""
    return list(HETEROGENEOUS_WORKERS.keys())


def get_workers_by_resource_tier(tier: str) -> List[WorkerProfile]:
    """Get all workers that prefer a specific resource tier."""
    return [w for w in HETEROGENEOUS_WORKERS.values() if w.resource_tier == tier]


def get_workers_by_model_preference(preference: str) -> List[WorkerProfile]:
    """Get all workers that prefer a specific model tier."""
    return [w for w in HETEROGENEOUS_WORKERS.values() if w.model_preference == preference]


def get_heterogeneity_metrics() -> Dict[str, Any]:
    """Compute metrics showing the heterogeneity of the worker pool."""
    workers = list(HETEROGENEOUS_WORKERS.values())
    
    # Tool diversity: how many unique tools across all workers
    all_tools = set()
    for w in workers:
        all_tools.update(w.tools)
    
    # Capability diversity
    all_capabilities = set()
    for w in workers:
        all_capabilities.update(w.capabilities)
    
    # Model preference distribution
    model_prefs = {}
    for w in workers:
        model_prefs[w.model_preference] = model_prefs.get(w.model_preference, 0) + 1
    
    # Resource tier distribution
    resource_tiers = {}
    for w in workers:
        resource_tiers[w.resource_tier] = resource_tiers.get(w.resource_tier, 0) + 1
    
    # Cost distribution
    costs = {}
    for w in workers:
        costs[w.cost_per_call] = costs.get(w.cost_per_call, 0) + 1
    
    # Tool overlap: how many tools are shared between workers
    tool_sharing = 0
    tool_unique = 0
    for w in workers:
        shared = 0
        for other in workers:
            if other.worker_type != w.worker_type:
                shared += len(set(w.tools) & set(other.tools))
        if shared == 0:
            tool_unique += 1
        else:
            tool_sharing += 1
    
    return {
        "total_worker_types": len(workers),
        "unique_tools": len(all_tools),
        "unique_capabilities": len(all_capabilities),
        "model_preference_distribution": model_prefs,
        "resource_tier_distribution": resource_tiers,
        "cost_distribution": costs,
        "workers_with_unique_tools": tool_unique,
        "workers_with_shared_tools": tool_sharing,
        "avg_tools_per_worker": sum(len(w.tools) for w in workers) / max(1, len(workers)),
        "avg_token_budget": sum(w.avg_token_budget for w in workers) / max(1, len(workers)),
    }
