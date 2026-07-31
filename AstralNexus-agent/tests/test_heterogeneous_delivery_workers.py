from AstralNexus.core.heterogeneous_workers import HETEROGENEOUS_WORKERS
from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


def test_delivery_workers_are_real_heterogeneous_profiles():
    architect = HETEROGENEOUS_WORKERS["Architect"]
    coding = HETEROGENEOUS_WORKERS["Coding"]
    testing = HETEROGENEOUS_WORKERS["Testing"]
    verifier = HETEROGENEOUS_WORKERS["Verifier"]
    synthesizer = HETEROGENEOUS_WORKERS["Synthesizer"]

    assert "system_architecture" in architect.capabilities
    assert "database_modeling" in architect.capabilities
    assert coding.model_preference == "coding_specialized"
    assert testing.model_preference == "testing_balanced"
    assert verifier.model_preference == "verifier_reasoning"
    assert "delivery_packaging" in synthesizer.capabilities


def test_reasoning_attributes_are_not_matchable_capabilities():
    forbidden = {"logical_reasoning", "analysis", "reasoning"}
    for profile in HETEROGENEOUS_WORKERS.values():
        assert not (forbidden & set(profile.capabilities))
        assert profile.reasoning_level in {"low", "medium", "high"}


def test_capability_policy_routes_distinct_delivery_workers(tmp_path):
    governor = GlobalGovernor(TaskGraph(), TraceRecorder(tmp_path))
    cases = [
        (["system_architecture", "api_design"], "Architect", "qwen3.7-max"),
        (["code_generation", "backend_development"], "Coding", "qwen3-coder-plus"),
        (["test_execution", "integration_testing"], "Testing", "qwen-plus"),
        (["evidence_check", "acceptance_test"], "Verifier", "qwen3.7-max"),
        (["artifact_integration", "delivery_packaging"], "Synthesizer", "qwen3.7-max"),
    ]
    for capabilities, worker_type, model in cases:
        decision = governor.select_worker_profile(
            {"task_id": worker_type, "required_capabilities": capabilities},
            HETEROGENEOUS_WORKERS,
        )
        assert decision["selected"]["worker_type"] == worker_type
        assert decision["selected"]["selected_model"] == model
