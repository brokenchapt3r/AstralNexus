"""Fix AO: the loop guard must not mistake a changed world for a loop.

The guard stops a worker repeating a call that cannot change anything.  Whether
a call can change anything depends on the state it reads -- which is why
``file_read`` already folded its target's content hash into the fingerprint.
Every other tool was fingerprinted on ``(name, arguments)`` alone, so re-running
the SAME build command after its driver script changed was indistinguishable
from re-running it against an unchanged world, and the third attempt was
refused.

Live regression (doc 18, r13b generation 19), from the Verifier's own verdict
on PATCH-R011-SERVE-ENTRY-LEDGER::

    worker 自述指向 000e5f8abb30…（79397 bytes）的调用被循环守卫拦截，
    现有 log/json/账本由更早驱动构建产出，最终构建从未被真实执行

The ledger kept a 20:05:38 computation while the log it registered was
rewritten at 20:08, and the node was correctly rejected for cross-round
inconsistency -- caused by a guard decision, not by the worker.

The guard is NOT relaxed: an identical call over identical state still blocks.
"""
from __future__ import annotations

from AstralNexus.runtime.tool_guardrails import ToolCallGuard


def _call(name="terminal_exec", **arguments):
    return {"function": {"name": name, "arguments": dict(arguments)}}


def _driver(tmp_path, body="print('v1')\n"):
    path = tmp_path / "deliverables" / "runs" / "runtime" / "run_serve_entry_verify.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- the live failure --------------------------------------------------------

def test_the_same_command_over_a_changed_script_is_a_new_call(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    driver = _driver(tmp_path)
    call = _call(command="python deliverables/runs/runtime/run_serve_entry_verify.py")
    before = guard._state_fingerprint(call)
    driver.write_text("print('v2 — the frozen build')\n", encoding="utf-8")
    assert guard._state_fingerprint(call) != before


def test_the_final_build_is_admitted_after_its_driver_changes(tmp_path):
    """Three attempts exhaust the budget; a changed driver must still be run."""
    guard = ToolCallGuard(workspace_root=tmp_path)
    driver = _driver(tmp_path)
    call = _call(command="python deliverables/runs/runtime/run_serve_entry_verify.py")
    for _ in range(4):
        admitted, _ = guard.admit(call)
        guard.observe(call, {"status": "success"})
    assert guard.admit(call)[0] is False, "precondition: the budget is spent"
    driver.write_text("print('final frozen build')\n", encoding="utf-8")
    admitted, reason = guard.admit(call)
    assert admitted is True, reason


# --- the guarantee that must survive ----------------------------------------

def test_an_identical_call_over_an_unchanged_world_still_blocks(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    _driver(tmp_path)
    call = _call(command="python deliverables/runs/runtime/run_serve_entry_verify.py")
    for _ in range(4):
        guard.admit(call)
        guard.observe(call, {"status": "success"})
    admitted, reason = guard.admit(call)
    assert admitted is False and reason == "repeated_identical_tool_call"


def test_a_call_referencing_nothing_on_disk_is_unchanged_in_behaviour(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    call = _call(command="echo hello")
    for _ in range(4):
        guard.admit(call)
        guard.observe(call, {"status": "success"})
    assert guard.admit(call)[0] is False


def test_file_read_keeps_its_original_content_awareness(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    target = tmp_path / "notes.md"
    target.write_text("one", encoding="utf-8")
    call = _call(name="file_read", path="notes.md")
    before = guard._state_fingerprint(call)
    target.write_text("two", encoding="utf-8")
    assert guard._state_fingerprint(call) != before


def test_a_path_outside_the_workspace_is_never_hashed(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path / "work")
    (tmp_path / "work").mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("x", encoding="utf-8")
    call = _call(command=f"python {outside}")
    assert guard._referenced_file_versions(call["function"]["arguments"]) == {}


def test_hashing_is_bounded(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    names = []
    for i in range(10):
        p = tmp_path / f"s{i}.py"
        p.write_text(f"print({i})", encoding="utf-8")
        names.append(f"s{i}.py")
    call = _call(command="python " + " ".join(names))
    assert len(guard._referenced_file_versions(call["function"]["arguments"])) <= 4


def test_a_missing_reference_does_not_raise(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    call = _call(command="python deliverables/gone.py")
    assert guard._referenced_file_versions(call["function"]["arguments"]) == {}
    assert isinstance(guard._state_fingerprint(call), str)


def test_string_encoded_arguments_are_still_parsed(tmp_path):
    guard = ToolCallGuard(workspace_root=tmp_path)
    _driver(tmp_path)
    call = {"function": {"name": "terminal_exec", "arguments":
            '{"command": "python deliverables/runs/runtime/run_serve_entry_verify.py"}'}}
    assert isinstance(guard._state_fingerprint(call), str)
