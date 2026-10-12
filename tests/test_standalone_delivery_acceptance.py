from __future__ import annotations

import hashlib
import json

from AstralNexus.core.delivery_acceptance import evaluate_delivery, write_acceptance_report


def _contract(run_dir, contract):
    (run_dir / "runtime_state.json").write_text(json.dumps({
        "governance_state": {"delivery_contract": contract},
    }), encoding="utf-8")


def test_gate_checks_staging_tree_and_rejects_undeclared_files(tmp_path):
    (tmp_path / "deliverables").mkdir()
    (tmp_path / "deliverables/a.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "a.txt").write_text("new", encoding="utf-8")
    (staging / "undeclared.txt").write_text("extra", encoding="utf-8")
    _contract(tmp_path, {
        "summary_required": False,
        "required_outputs": [{"type": "file", "required": True, "path": "deliverables/a.txt"}],
    })

    report = evaluate_delivery(tmp_path)

    assert report["candidate"]["kind"] == "staging"
    assert report["status"] == "fail"
    scope = next(item for item in report["checks"] if item["check_id"] == "DG-SCOPE")
    assert "undeclared.txt" in scope["evidence_refs"]


def test_gate_accepts_exact_artifact_first_candidate_without_text(tmp_path):
    staging = tmp_path / ".delivery_staging" / "app"
    staging.mkdir(parents=True)
    (staging / "main.py").write_text("print('ok')\n", encoding="utf-8")
    _contract(tmp_path, {
        "summary_required": False,
        "execution_evidence_policy": "plan_only",
        "required_sections": [],
        "required_outputs": [{
            "output_id": "O-01", "name": "application", "type": "software",
            "required": True, "path": "deliverables/app",
        }],
    })

    report = evaluate_delivery(tmp_path)

    assert report["status"] == "pass"
    assert report["delivery_authorized"] is True


def test_gate_enforces_exact_sections_and_forbidden_claims(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "delivery_response.md").write_text(
        "正文提到必须章节，但并不是标题。\n已部署生产。\n", encoding="utf-8"
    )
    _contract(tmp_path, {
        "summary_required": True,
        "required_outputs": [{"name": "answer", "type": "text", "required": True}],
        "required_sections": ["必须章节"],
        "forbidden_claims": ["已部署生产"],
    })

    report = evaluate_delivery(tmp_path)

    assert report["status"] == "fail"
    assert next(item for item in report["checks"] if item["check_id"] == "DG-SECTIONS")["status"] == "fail"
    assert next(item for item in report["checks"] if item["check_id"] == "DG-FORBIDDEN")["status"] == "fail"


def test_gate_fails_tampered_published_manifest(tmp_path):
    version = tmp_path / "deliverables" / "v1"
    version.mkdir(parents=True)
    artifact = version / "a.txt"
    artifact.write_text("changed", encoding="utf-8")
    (version / "delivery_manifest.json").write_text(json.dumps({
        "status": "published",
        "version": 1,
        "artifacts": [{"path": "a.txt", "sha256": hashlib.sha256(b"original").hexdigest()}],
    }), encoding="utf-8")
    (tmp_path / "deliverables/current.json").write_text(
        json.dumps({"current_version": 1, "path": "v1"}), encoding="utf-8"
    )
    _contract(tmp_path, {
        "summary_required": False,
        "required_outputs": [{"type": "file", "required": True, "path": "deliverables/a.txt"}],
    })

    report = evaluate_delivery(tmp_path)

    manifest = next(item for item in report["checks"] if item["check_id"] == "DG-MANIFEST")
    assert manifest["status"] == "fail"
    assert "sha256 mismatch" in manifest["detail"]


def test_legacy_conditional_output_is_not_promoted_to_required(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "delivery_response.md").write_text("# Result\n\nDone.\n", encoding="utf-8")
    _contract(tmp_path, {
        "summary_required": True,
        "required_outputs": [{
            "type": "file", "source": "conditional", "condition": "when enabled",
            "path": "deliverables/optional.txt",
        }],
    })

    report = evaluate_delivery(tmp_path)

    output = next(item for item in report["checks"] if item["check_id"] == "DG-OUTPUT-01")
    assert output["status"] == "pass"
    assert "optional" in output["detail"]


def test_undecidable_hard_criterion_is_inconclusive_not_pass(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "delivery_response.md").write_text("# Answer\n", encoding="utf-8")
    _contract(tmp_path, {
        "summary_required": True,
        "required_outputs": [{"name": "answer", "type": "text", "required": True}],
        "global_acceptance_criteria": ["答案必须具有卓越的商业洞察"],
    })

    report = evaluate_delivery(tmp_path)

    assert report["status"] == "inconclusive"
    assert report["delivery_authorized"] is False


def test_missing_contract_is_inconclusive_instead_of_scope_failure(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "result.txt").write_text("result", encoding="utf-8")

    report = evaluate_delivery(tmp_path)

    assert report["status"] == "inconclusive"
    assert next(item for item in report["checks"] if item["check_id"] == "DG-SCOPE")["status"] == "unknown"


def test_summary_required_and_hard_constraints_are_enforced(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "acceptance_report.md").write_text("gate input", encoding="utf-8")
    _contract(tmp_path, {
        "summary_required": True,
        "execution_evidence_policy": "plan_only",
        "hard_constraints": ["answer must demonstrate exceptional judgment"],
        "required_outputs": [],
    })

    report = evaluate_delivery(tmp_path)

    assert report["status"] == "fail"
    assert next(item for item in report["checks"] if item["check_id"] == "DG-SUMMARY")["status"] == "fail"
    assert next(item for item in report["checks"] if item["check_id"] == "DG-HARD-01")["status"] == "unknown"


def test_published_manifest_escape_is_rejected_without_reading_outside(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    version = tmp_path / "deliverables" / "v1"
    version.mkdir(parents=True)
    (version / "delivery_manifest.json").write_text(json.dumps({
        "status": "published",
        "version": 1,
        "artifacts": [{"path": "../../outside.txt", "sha256": hashlib.sha256(b"secret").hexdigest()}],
    }), encoding="utf-8")
    (tmp_path / "deliverables/current.json").write_text(
        json.dumps({"current_version": 1, "path": "v1"}), encoding="utf-8"
    )
    _contract(tmp_path, {"summary_required": False, "required_outputs": []})

    report = evaluate_delivery(tmp_path)

    manifest = next(item for item in report["checks"] if item["check_id"] == "DG-MANIFEST")
    assert manifest["status"] == "fail"
    assert "escapes candidate" in manifest["detail"]


def test_report_cannot_mutate_evaluated_candidate(tmp_path):
    staging = tmp_path / ".delivery_staging"
    staging.mkdir()
    (staging / "result.txt").write_text("result", encoding="utf-8")
    _contract(tmp_path, {
        "execution_evidence_policy": "plan_only",
        "required_outputs": [{"type": "file", "required": True, "path": "deliverables/result.txt"}],
    })
    report = evaluate_delivery(tmp_path)

    try:
        write_acceptance_report(report, staging / "report.json")
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("writing into the accepted candidate must be rejected")



def test_explicit_candidate_accepts_path_and_string_without_object_kind(tmp_path):
    from AstralNexus.core.delivery_acceptance import resolve_delivery_candidate
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for value in (candidate, str(candidate)):
        resolved = resolve_delivery_candidate(tmp_path, value)
        assert resolved.root == candidate.resolve()
        assert resolved.kind == "explicit"
