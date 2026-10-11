"""One execution vocabulary across planning, guard identity and acceptance."""
import json

import pytest

from AstralNexus.runtime.acceptance_checks import (
    EXECUTION_CRITERION_GUIDANCE, bind_criterion_paths,
    render_execution_contract, run_acceptance_checks,
)
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.verification_identity import declared_execution_commands

CMD = "python deliverables/app/main.py 2 3"


def claim(stdout="5", command=CMD):
    return "command_output:" + json.dumps({"command": command, "stdout": stdout})


def receipt(**overrides):
    result = {"tool": "terminal_exec", "status": "success", "returncode": 0,
              "stdout_tail": "5\n", "receipt_id": "real", "files": {},
              "arguments": json.dumps({"command": CMD})}
    result.update(overrides)
    return result


@pytest.mark.parametrize("criterion,holds,reason", [
    (claim(), True, ""),
    ("command_succeeded:" + CMD, True, ""),
    ("tests_passed:" + CMD, None, "TEST_SUMMARY_NOT_RECOGNIZED"),
    (claim("6"), False, "COMMAND_OUTPUT_MISMATCH"),
    ('command_output:{"command":"x"}', False, "INVALID_EXECUTION_CRITERION"),
])
def test_ordinary_output_is_not_a_test_summary(criterion, holds, reason):
    result = run_acceptance_checks([criterion], receipts=[receipt()])[0]
    assert result["holds"] is holds
    assert result["reason"] == reason


@pytest.mark.parametrize("change,reason", [
    ({"returncode": None}, "COMMAND_EXIT_CODE_UNAVAILABLE"),
    ({"returncode": 1, "status": "error"}, "TEST_EXECUTION_FAILED"),
    ({"arguments": json.dumps({"command": CMD + " extra"})}, "TEST_EXECUTION_NOT_FOUND"),
    ({"arguments": json.dumps({"command": 'echo "' + CMD + '"'})}, "TEST_EXECUTION_NOT_FOUND"),
    ({"status": "budget_exhausted", "returncode": None}, "TEST_EXECUTION_BLOCKED"),
])
def test_ordinary_claim_preserves_execution_guards(change, reason):
    result = run_acceptance_checks([claim()], receipts=[receipt(**change)])[0]
    assert result["holds"] is not True
    assert result["reason"] == reason


def test_output_claim_shares_admission_and_guard_identity():
    bound, _ = bind_criterion_paths([claim(command="python main.py 2 3")],
                                   declared=["deliverables/app/main.py"])
    assert json.loads(bound[0].split(":", 1)[1])["command"] == CMD
    assert declared_execution_commands(bound) == {CMD: bound[0]}
    assert EXECUTION_CRITERION_GUIDANCE in render_execution_contract(bound)
    assert run_acceptance_checks(bound, receipts=[receipt()])[0]["holds"] is True


@pytest.mark.parametrize("criterion,output,accepted", [
    (claim(), "5\n", True), (claim(), "6\n", False),
    ("command_succeeded:" + CMD, "", True),
    ("tests_passed:" + CMD, "5\n", False),
])
def test_execution_store_hard_gate_uses_same_claim_kinds(tmp_path, criterion, output, accepted):
    store = ExecutionStore(tmp_path)
    execution = store.begin("adder", {"owned_paths": [], "acceptance_criteria": [criterion]})
    store.append(execution, receipt(stdout_tail=output))
    store.finish(execution, "succeeded", {"owned_files": {}})
    if accepted:
        store.accept(execution, {"approved": True})
        assert store.get(execution)["acceptance"]["approved"] is True
    else:
        with pytest.raises(ValueError):
            store.accept(execution, {"approved": True})


def test_ordinary_command_changed_product_invalidates_old_result():
    result = run_acceptance_checks([claim()], receipts=[receipt(files={
        "deliverables/app/main.py": {"sha256": "old"},
    })], latest_files={"deliverables/app/main.py": {"sha256": "new"}})[0]
    assert result["holds"] is False
    assert result["reason"] == "TEST_EXECUTION_STALE"


def test_stderr_failure_vetoes_stdout_passing_summary():
    result = run_acceptance_checks(["tests_passed:" + CMD], receipts=[receipt(
        stdout_tail="3 passed", stderr_tail="FAILED (failures=1)",
    )])[0]
    assert result["holds"] is False
    assert result["reason"] == "TESTS_REPORTED_FAILURES"


def test_recorded_cwd_is_part_of_executed_script_identity(tmp_path):
    called = receipt(arguments=json.dumps({"command": "python main.py 2 3"}),
                     raw_result={"cwd": str(tmp_path / "deliverables/app"), "stdout": "5\n"})
    assert run_acceptance_checks([claim()], receipts=[called], root=tmp_path)[0]["holds"] is True
    called["raw_result"]["cwd"] = str(tmp_path / "unrelated")
    result = run_acceptance_checks([claim()], receipts=[called], root=tmp_path)[0]
    assert result["reason"] == "TEST_EXECUTION_NOT_FOUND"
    called["raw_result"]["cwd"] = str(tmp_path.parent)
    assert run_acceptance_checks([claim()], receipts=[called], root=tmp_path)[0]["holds"] is not True


def test_truncated_tail_does_not_prove_full_stdout():
    tail = "x" * 1500
    observation = run_acceptance_checks([claim(tail)], receipts=[receipt(stdout_tail=tail)])[0]
    assert observation["reason"] == "COMMAND_OUTPUT_UNAVAILABLE"
    observation = run_acceptance_checks([claim(tail)], receipts=[receipt(
        stdout_tail=tail, raw_result={"stdout": "hidden-prefix" + tail},
    )])[0]
    assert observation["reason"] == "COMMAND_OUTPUT_MISMATCH"


def test_ordinary_output_failure_has_targeted_repair_without_changing_expectation():
    from AstralNexus.runtime.development_playbook import recovery_procedures
    observation = run_acceptance_checks([claim()], receipts=[receipt(stdout_tail="6\n")])[0]
    repair = recovery_procedures([observation])[0]
    assert repair["route"] == "repair_implementation"
    assert repair["recheck_command"] == CMD
    assert repair["criterion"] == claim()


def test_readonly_consumer_binds_upstream_artifact_and_terminal_newline(tmp_path):
    criteria = [claim("5\n", "python main.py 2 3")]
    upstream = [str(tmp_path / "deliverables/app/main.py")]
    result = run_acceptance_checks(criteria, receipts=[receipt()], root=tmp_path,
                                   declared_premises=upstream)[0]
    assert result["holds"] is True
    commands = declared_execution_commands(criteria, declared=upstream, root=tmp_path)
    assert list(commands) == [CMD]
    assert CMD in render_execution_contract(criteria, declared=upstream, root=tmp_path)


def test_unittest_discovery_binds_declared_suite_not_a_narrower_pattern(tmp_path):
    criterion = "tests_passed:python -m unittest -v"
    upstream = ["deliverables/app/test_main.py"]
    command = "python -m unittest discover -s deliverables/app -p test_*.py -v"
    called = receipt(arguments=json.dumps({"command": command}), stdout_tail="",
                     stderr_tail="Ran 3 tests in 0.2s\n\nOK")
    result = run_acceptance_checks([criterion], receipts=[called], root=tmp_path,
                                   declared_premises=upstream)[0]
    assert result["holds"] is True
    called["arguments"] = json.dumps({"command": command.replace("test_*.py", "test_one.py")})
    result = run_acceptance_checks([criterion], receipts=[called], root=tmp_path,
                                   declared_premises=upstream)[0]
    assert result["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_unittest_discovery_options_are_canonicalized_before_execution():
    raw = "tests_passed:python -m unittest -v -s deliverables/app -p test_main.py"
    bound, _ = bind_criterion_paths([raw], declared=["deliverables/app/test_main.py"])
    assert "unittest discover" in bound[0]
    called = receipt(arguments=json.dumps({"command":
        "python -m unittest discover -s deliverables/app -p test_main.py -v"}),
        stdout_tail="", stderr_tail="Ran 3 tests in 0.2s\n\nOK")
    assert run_acceptance_checks(bound, receipts=[called])[0]["holds"] is True



def test_premises_use_structured_upstream_paths_not_dict_repr_or_stdout():
    from AstralNexus.runtime.verification_identity import declared_premise_paths
    values = declared_premise_paths(
        acceptance_criteria=['file:<deliverables/app/main.py> exists', 'command_output:{"command":"python main.py","stdout":"5\\n"}'],
        upstream_artifacts=[{"path": "deliverables/app/main.py", "acceptance_status": "accepted"},
                            {"path": "rejected.py", "acceptance_status": "rejected"}],
    )
    assert values == ["deliverables/app/main.py"]



def test_file_contains_is_portable_literal_and_false_for_missing_text(tmp_path):
    import json
    from AstralNexus.runtime.acceptance_checks import run_acceptance_checks, bind_criterion_paths, criterion_paths
    doc = tmp_path / "deliverables/app/README.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Running\npython main.py <a> <b>\n# Testing\npython -m unittest -v\n", encoding="utf-8")
    def claim(text):
        return "file_contains:" + json.dumps({"path": "README.md", "text": text})
    bound, _ = bind_criterion_paths([claim("python -m unittest -v"), claim("not-present.*")],
                                  declared=["deliverables/app/README.md"], root=tmp_path)
    assert criterion_paths(bound[0]) == ["deliverables/app/README.md"]
    observations = run_acceptance_checks(bound, receipts=[], execution_id="offline", root=tmp_path)
    assert observations[0]["holds"] is True
    assert observations[1]["holds"] is False
    assert observations[1]["reason"] == "FILE_CONTENT_MISMATCH"
