from AstralNexus.core.orchestrator import Orchestrator


def _terminal_root(orchestrator):
    root_id = orchestrator.initialize_task_graph("build a verified artifact", [])
    node = orchestrator.task_graph.get_node(root_id)
    node.status = "completed"
    orchestrator.governor.delivery_contract = {
        "required_outputs": [],
        "global_acceptance_criteria": [],
    }
    return root_id


def test_terminal_subgraph_expands_when_synthesizer_is_missing(tmp_path, monkeypatch):
    orchestrator = Orchestrator(tmp_path)
    _terminal_root(orchestrator)
    monkeypatch.setattr(
        orchestrator.governor,
        "verify_delivery",
        lambda *args, **kwargs: {
            "overall_status": "pass",
            "synthesis_authorized": True,
            "coverage_gaps": [],
            "failed_tests": [],
        },
    )
    captured = {}

    def plan(node_id, *, planning_goal=None, allow_completed_parent=False):
        captured["goal"] = planning_goal
        captured["allow_completed_parent"] = allow_completed_parent
        return ["synthesis-node"]

    monkeypatch.setattr(orchestrator, "plan_task_decomposition", plan)
    result = orchestrator._assess_delivery_completion("build a verified artifact")

    assert result["authorized"] is False
    assert result["changed"] is True
    assert captured["allow_completed_parent"] is True
    assert "Synthesizer" in captured["goal"]


def test_completion_requires_completed_synthesizer_and_verifier_pass(
    tmp_path, monkeypatch
):
    orchestrator = Orchestrator(tmp_path)
    root_id = _terminal_root(orchestrator)
    orchestrator.task_graph.add_node(
        {
            "node_id": "synth",
            "goal": "integrate delivery",
            "role": "synthesizer",
            "status": "completed",
            "dependencies": [root_id],
            "metadata": {"worker_profile": {"worker_type": "Synthesizer"}},
        }
    )
    monkeypatch.setattr(
        orchestrator.governor,
        "verify_delivery",
        lambda *args, **kwargs: {
            "overall_status": "pass",
            "synthesis_authorized": True,
            "coverage_gaps": [],
            "failed_tests": [],
        },
    )

    result = orchestrator._assess_delivery_completion("build a verified artifact")

    assert result["authorized"] is True
