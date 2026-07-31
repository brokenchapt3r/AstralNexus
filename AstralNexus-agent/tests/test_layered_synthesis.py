import json
from pathlib import Path

from AstralNexus.core.layered_synthesis import build_layered_context


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if "交付契约" in prompt and "只输出 JSON" in prompt:
            return json.dumps({
                "delivery_mode": "text",
                "required_outputs": ["交付文本"],
                "required_sections": ["结论"],
                "global_acceptance_criteria": ["直接回答任务"],
                "forbidden_claims": [],
            }, ensure_ascii=False)
        if "全局上下文治理器" in prompt:
            return json.dumps({
                "groups": [
                    {"group_id": "G-trend", "title": "趋势", "node_ids": ["T-001"]},
                    {"group_id": "G-risk", "title": "风险", "node_ids": ["T-002"]},
                ],
                "actions": [
                    {"node_id": "T-001", "action": "retain"},
                    {"node_id": "T-002", "action": "compress"},
                ],
            }, ensure_ascii=False)
        if "生成适合该任务" in prompt:
            assert "GLOBAL-VERIFIED" in prompt
            return "# L3 大纲\n- 证据引用\n- 冲突与待核验项"
        return json.dumps({
            "group_id": "model-filled",
            "source_nodes": [],
            "verified_claims": [{
                "claim": "GLOBAL-VERIFIED",
                "evidence_refs": ["node-output"],
                "confidence": 0.9,
                "epistemic_status": "observed",
            }],
            "conflicts": [],
            "coverage_gaps": [],
            "constraints": [],
            "limitations": [],
            "open_questions": [],
            "recommended_followups": [],
            "source_artifacts": [],
            "task_version": 1,
        }, ensure_ascii=False)


class FakeGovernor:
    def __init__(self):
        self.global_verifier_called = False

    def derive_delivery_contract(self, task_content, client):
        return {
            "delivery_mode": "text",
            "required_outputs": ["交付文本"],
            "required_sections": ["结论"],
            "global_acceptance_criteria": [],
            "forbidden_claims": [],
            "needs_citations": False,
            "needs_artifact_refs": True,
        }

    def plan_context_governance(self, task_content, packets, client):
        from AstralNexus.core.governor import ContextGovernanceDecision
        return ContextGovernanceDecision(
            groups=[
                {"group_id": "G-trend", "title": "趋势", "node_ids": ["T-001"]},
                {"group_id": "G-risk", "title": "风险", "node_ids": ["T-002"]},
            ],
            retained_packet_ids=["T-001"],
            compressed_packet_ids=["T-002"],
        )

    def verify_evidence_summaries(
        self,
        contract,
        reducers,
        *,
        artifact_refs,
        task_version,
        run_dir,
    ):
        self.global_verifier_called = True
        assert reducers
        assert task_version == 1
        assert run_dir
        return {
            "overall_status": "pass",
            "synthesis_authorized": True,
            "supported_claims": ["GLOBAL-VERIFIED"],
            "unsupported_claims": [],
            "conflicts": [],
            "coverage_gaps": [],
            "required_fixes": [],
            "evidence_refs": artifact_refs,
        }


def test_build_layered_context_keeps_raw_artifacts_and_bounds_inputs(tmp_path: Path):
    node_dir = tmp_path / "node_io"
    node_dir.mkdir()
    for node_id, role in (("T-001", "researcher"), ("T-002", "reviewer")):
        (node_dir / f"{node_id}_output.json").write_text(json.dumps({
            "node_id": node_id,
            "role": role,
            "goal": f"目标 {node_id}",
            "execution_outcome": "succeeded",
            "content": "证据内容 " + ("x" * 10000),
        }), encoding="utf-8")

    client = FakeClient()
    governor = FakeGovernor()
    result = build_layered_context(
        task_content="研究任务",
        run_dir=tmp_path,
        client=client,
        governor=governor,
        reducer_workers=2,
    )

    assert len(result["packets"]) == 2
    assert len(result["groups"]) == 2
    assert len(result["approved_packets"]) == 2
    assert result["outline"].startswith("# L3")
    assert governor.global_verifier_called is True
    assert result["global_verification"]["synthesis_authorized"] is True
    assert (tmp_path / "layered" / "evidence_packets" / "T-001.json").exists()
    assert (tmp_path / "layered" / "reducers" / "G-trend.md").exists()
    assert (tmp_path / "layered" / "outline.md").exists()
    assert (tmp_path / "layered" / "global_verification.json").exists()
    assert all(len(call) < 20000 for call in client.calls)
