"""Report backfill cannot invalidate tests; real execution inputs still can."""
import json
import subprocess
import sys
import pytest

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.execution_ledger import ExecutionLedger
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.trace import TraceRecorder
from AstralNexus.runtime.verification_identity import declared_premise_paths, premise_versions


def _verified_then_reported(root):
    app = root / "deliverables/app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("VALUE = 5\n", encoding="utf-8")
    (app / "test_main.py").write_text("import unittest\nclass Check(unittest.TestCase):\n def test_value(self):\n  import main\n  self.assertEqual(main.VALUE, 5)\n", encoding="utf-8")
    report = app / "verification_report.json"
    report.write_text('{"phase":"before"}', encoding="utf-8")
    criteria = ["tests_passed:python -m unittest discover -s deliverables/app -v"]
    store = ExecutionStore(root)
    execution = store.begin("reverifier", {"owned_paths": ["deliverables/app"], "acceptance_criteria": criteria})
    ledger = ExecutionLedger(root, ["deliverables/app"], execution)
    command = ["python", "-m", "unittest", "discover", "-s", "deliverables/app", "-v"]
    process = subprocess.run([sys.executable, *command[1:]], cwd=root, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    ledger.observe({"id": "test", "function": {"name": "terminal_exec", "arguments": {"command": command}}},
                   {"status": "success", "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
    report.write_text('{"passed":true}', encoding="utf-8")
    ledger.observe({"id": "report", "function": {"name": "file_write", "arguments": {"path": "deliverables/app/verification_report.json", "content": report.read_text()}}},
                   {"status": "success", "path": str(report)})
    node = TaskNode(node_id="R", role="verifier", goal="repeat independent verification", acceptance_criteria=criteria,
                    metadata={"runtime_execution_id": execution, "recovery_of": "V", "owned_paths": ["deliverables/app"],
                              "expected_outputs": ["deliverables/app/verification_report.json"]})
    verifier = Verifier(TaskGraph(), TraceRecorder(root), workspace_root=root)
    return app, node, verifier


def test_report_write_after_passing_test_keeps_valid_receipt(tmp_path):
    app, node, verifier = _verified_then_reported(tmp_path)
    observations = verifier.deterministic_observations(node)
    assert all(item["holds"] is True for item in observations), observations
    assert not any(item["reason"].startswith("WRITE_") for item in observations)
    versions = premise_versions(tmp_path, ["deliverables/app"])
    assert "deliverables/app/verification_report.json" not in versions
    assert "deliverables/app/main.py" in versions


@pytest.mark.parametrize("path", ["main.py", "test_main.py"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_product_or_test_change_still_invalidates_receipt(tmp_path, path, change):
    app, node, verifier = _verified_then_reported(tmp_path)
    target = app / path
    if change == "delete":
        target.unlink()
    else:
        target.write_text(target.read_text() + "\n# modified\n", encoding="utf-8")
    observations = verifier.deterministic_observations(node)
    assert any(item["reason"] == "TEST_EXECUTION_STALE" and item["holds"] is False for item in observations)


def test_missing_report_still_blocks_node(tmp_path):
    app, node, verifier = _verified_then_reported(tmp_path)
    (app / "verification_report.json").unlink()
    observations = verifier.deterministic_observations(node)
    assert next(item for item in observations if item["kind"] == "tests_passed")["holds"] is True
    assert any(item["reason"] == "FILE_NOT_FOUND" and item["holds"] is False for item in observations)


def test_coder_reverification_does_not_invent_a_product_write_obligation(tmp_path):
    app, node, verifier = _verified_then_reported(tmp_path)
    node.role = "coder"
    node.metadata["responsibility_scope"] = "reverification_only"
    observations = verifier.deterministic_observations(node)
    assert all(item["holds"] is True for item in observations), observations
    assert not any(item["reason"].startswith("WRITE_") for item in observations)


def test_report_explicitly_consumed_by_command_remains_a_versioned_input(tmp_path):
    from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
    (tmp_path / "consume.py").write_text("import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text())\n", encoding="utf-8")
    (tmp_path / "summary.json").write_text("before", encoding="utf-8")
    criteria = ["command_succeeded:python consume.py summary.json"]
    contract = {"owned_paths": ["consume.py", "summary.json"], "acceptance_criteria": criteria}
    store = ExecutionStore(tmp_path)
    execution = store.begin("reader", contract)
    ledger = ExecutionLedger(tmp_path, contract["owned_paths"], execution)
    command = ["python", "consume.py", "summary.json"]
    process = subprocess.run([sys.executable, *command[1:]], cwd=tmp_path, capture_output=True, text=True)
    ledger.observe({"id": "read-report", "function": {"name": "terminal_exec", "arguments": {"command": command}}},
                   {"status": "success", "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
    declared = declared_premise_paths(criteria, owned_paths=contract["owned_paths"], root=tmp_path)
    assert "summary.json" in declared
    (tmp_path / "summary.json").write_text("after", encoding="utf-8")
    observations = run_acceptance_checks(criteria, receipts=store.get(execution)["receipts"], root=tmp_path,
        premise_versions=premise_versions(tmp_path, declared), declared_premises=declared)
    assert observations[0]["reason"] == "TEST_EXECUTION_STALE", observations


def test_source_under_report_directory_is_not_excluded(tmp_path):
    path = tmp_path / "reports/report.py"
    path.parent.mkdir()
    path.write_text("print(1)", encoding="utf-8")
    assert "reports/report.py" in premise_versions(tmp_path, ["reports"])


def test_explicit_upstream_report_input_still_invalidates_implicit_reader(tmp_path):
    from AstralNexus.runtime.acceptance_checks import run_acceptance_checks
    from AstralNexus.runtime.execution_store import snapshot_files
    (tmp_path / "consume.py").write_text("from pathlib import Path\nprint(Path('summary.json').read_text())\n", encoding="utf-8")
    report = tmp_path / "summary.json"
    report.write_text("before", encoding="utf-8")
    criteria = ["command_succeeded:python consume.py"]
    contract = {"acceptance_criteria": criteria, "owned_paths": ["consume.py"],
                "upstream_artifacts": [{"path": "summary.json", "acceptance_status": "accepted"}]}
    store = ExecutionStore(tmp_path)
    execution = store.begin("reader", contract)
    ledger = ExecutionLedger(tmp_path, contract["owned_paths"], execution)
    process = subprocess.run([sys.executable, "consume.py"], cwd=tmp_path, capture_output=True, text=True)
    ledger.observe({"id": "read", "function": {"name": "terminal_exec", "arguments": {"command": ["python", "consume.py"]}}},
                   {"status": "success", "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
    declared = declared_premise_paths(criteria, contract["upstream_artifacts"], contract["owned_paths"], root=tmp_path)
    assert "summary.json" in declared
    report.unlink()
    observations = run_acceptance_checks(criteria, receipts=store.get(execution)["receipts"], root=tmp_path,
        premise_versions=premise_versions(tmp_path, declared), declared_premises=declared,
        latest_files=snapshot_files(tmp_path, declared))
    assert observations[0]["reason"] == "TEST_EXECUTION_STALE", observations


@pytest.mark.parametrize("role", ["coder", "verifier"])
def test_explicit_product_write_obligation_cannot_skip_write_gate(tmp_path, role):
    app, node, verifier = _verified_then_reported(tmp_path)
    node.role = role
    node.metadata["responsibility_scope"] = "reverification_only"
    node.acceptance_criteria.append("file_written:deliverables/app/main.py")
    observations = verifier.deterministic_observations(node)
    assert any(item["reason"] == "WRITE_ONLY_REPORT_CHANGE" for item in observations)
