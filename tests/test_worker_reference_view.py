import hashlib
import json
from copy import deepcopy

import pytest

from AstralNexus.agents.base import AgentContext
from AstralNexus.agents.coder import Coder
from AstralNexus.runtime.worker_reference_view import project_worker_references


def payload(tmp_path):
    artifacts = [{"artifact_id": f"a-{i}", "path": f"archive/{i}.json",
                  "acceptance_status": "rejected", "description": "history" * 100}
                 for i in range(492)]
    return {"run_dir": str(tmp_path), "upstream_artifacts": artifacts,
            "task_contract": {"acceptance_criteria": ["required-proof"],
                              "context": {"relevant_artifacts": artifacts,
                                          "constraints": ["no weakening"]}},
            "recovery_handoff": {"failure": "missing current proof"},
            "acceptance_criteria": ["required-proof"], "owned_paths": ["output/"]}


def test_real_prompt_references_preserve_full_inventory_and_active_contract(tmp_path):
    metadata = payload(tmp_path)
    original = deepcopy(metadata)
    prompt = Coder()._build_runtime_prompt(AgentContext(
        node_id="repair", goal="repair targeted defect", metadata=metadata), "implementation")
    assert len(prompt) < 25000
    assert "missing current proof" in prompt
    assert "required-proof" in prompt and "no weakening" in prompt
    assert "output/" in prompt and "repair targeted defect" in prompt
    assert metadata == original
    files = list((tmp_path / ".astralnexus_state/worker_references").glob("*.json"))
    assert len(files) == 1  # identical inventories share their content address
    assert json.loads(files[0].read_text(encoding="utf-8")) == original["upstream_artifacts"]
    assert files[0].stem == hashlib.sha256(files[0].read_bytes()).hexdigest()


def test_small_inventory_stays_inline_without_files(tmp_path):
    metadata = {"upstream_artifacts": [{"path": "proof.json"}]}
    assert project_worker_references(metadata, tmp_path) == metadata
    assert not list(tmp_path.iterdir())


def test_corrupt_reference_never_silently_used(tmp_path):
    metadata = payload(tmp_path)
    view = project_worker_references(metadata, tmp_path)
    path = tmp_path / view["upstream_artifacts"]["path"]
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        project_worker_references(metadata, tmp_path)
