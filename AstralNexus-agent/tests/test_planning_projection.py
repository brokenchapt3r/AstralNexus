import copy
import json

from AstralNexus.runtime.planning_projection import project_planning_graph


def test_retired_contracts_compact_without_losing_live_dependencies_or_ownership():
    records = [
        {"node_id": "live", "status": "blocked", "dependencies": ["old-needed"], "goal": "Keep full current requirement"},
        {"node_id": "old-needed", "status": "superseded", "dependencies": ["ancestor"], "goal": "Still a dependency"},
        {"node_id": "ancestor", "status": "cancelled", "dependencies": [], "goal": "Transitive dependency"},
        {"node_id": "old", "status": "superseded", "dependencies": [], "goal": "stale requirement " * 1000,
         "acceptance_criteria": ["old criterion"], "owned_paths": ["backend"], "output_artifacts": ["A1"]},
    ]
    original = copy.deepcopy(records)
    result = project_planning_graph(records)
    assert result[:3] == original[:3]
    assert result[3]["owned_paths"] == ["backend"]
    assert result[3]["output_artifacts"] == ["A1"]
    assert "goal" not in result[3]
    assert "acceptance_criteria" in result[3]["historical_contract"]["omitted_fields"]
    assert len(json.dumps(result)) < len(json.dumps(original)) / 4
    assert records == original
    assert project_planning_graph(records, focus_ids=("old",)) == original
