"""Fix AN: the evidence packet must not hide the evidence it is judging.

Live regression (doc 18, r13b generation 18).  ``PATCH-R010-SERVE-ENTRY``
failed verification twice -- 0.8333 then 0.8095 -- on blocking gaps that were
artifacts of the packet, not of the node's work::

    AC01末句/AC05未核验：deliverables/runs/runtime/serve_entry_verify.log
    （确定性检查仅确认非空 108437 bytes）的正文未出现在证据包中，无法逐字
    核对 'MAINT_READY port=8185 db=<PATH>'、对应 GET /health 的访问日志行、
    'MAINT_STOPPED' 三个字面行

    AC06：startup_run1.log、restart_run2.log、runtime_selfcheck.log
    既无内容也无 sha256

Two causes, both in ``_verify_semantic_goal_coverage``:

1. ``handle.read(limit)`` returned only the HEAD.  Runtime logs are
   append-only, so the round being judged writes at the TAIL -- the packet
   showed the oldest rounds and structurally hid the newest.
2. The budget was first-come-first-served and the loop ``break``-ed at
   exhaustion, so one large file starved every later file into total silence.

Together these made some acceptance criteria unsatisfiable by construction --
the Fix T/U defect class, one layer down.  The total budget is unchanged; it is
shared fairly, truncation shows both ends, and every candidate is listed with
code-computed bytes/sha256.
"""
from __future__ import annotations

import hashlib

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder

HEAD_MARK = "ROUND-1-OLDEST-LINE"
TAIL_MARKS = ("MAINT_READY port=8185", '"GET /health HTTP/1.1" 200', "MAINT_STOPPED")


def _verifier(tmp_path):
    return Verifier(TaskGraph(), TraceRecorder(tmp_path), llm_client=None,
                    workspace_root=tmp_path)


def _packet(tmp_path, node):
    """Build the packet the way _verify_semantic_goal_coverage does."""
    captured = {}
    v = _verifier(tmp_path)

    class _Client:
        def chat(self, messages, response_format=None):
            import json as _json
            captured["payload"] = _json.loads(messages[1]["content"])
            return '{"score": 1.0, "details": "ok", "blocking_gaps": []}'

    v.llm_client = _Client()
    v._verify_semantic_goal_coverage(node)
    return captured["payload"]


def _by_path(payload):
    return {item["path"]: item for item in payload["actual_files"]}


def _big_log(path, tail_lines):
    """A 108 KB append-only log: old rounds first, this round's proof last."""
    filler = "\n".join(f"{HEAD_MARK} {i:05d} " + "x" * 80 for i in range(1200))
    path.write_text(filler + "\n" + "\n".join(tail_lines) + "\n", encoding="utf-8")
    return path


def _node(tmp_path, owned="deliverables/runs/runtime/"):
    return TaskNode(node_id="PATCH-R010-SERVE-ENTRY", goal="runtime entry", role="coder",
                    metadata={"owned_paths": [owned]})


# --- the live failure --------------------------------------------------------

def test_the_tail_of_a_large_log_reaches_the_verifier(tmp_path):
    """The lines the criteria name are written last; they must be visible."""
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    _big_log(runtime / "serve_entry_verify.log", TAIL_MARKS)
    entry = _by_path(_packet(tmp_path, _node(tmp_path)))["deliverables/runs/runtime/serve_entry_verify.log"]
    assert entry["truncated"] is True and entry["excerpt"] == "head+tail"
    for mark in TAIL_MARKS:
        assert mark in entry["content"], mark


def test_the_head_is_still_shown(tmp_path):
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    _big_log(runtime / "serve_entry_verify.log", TAIL_MARKS)
    entry = _by_path(_packet(tmp_path, _node(tmp_path)))["deliverables/runs/runtime/serve_entry_verify.log"]
    assert HEAD_MARK in entry["content"]
    assert "未展示" in entry["content"], "the elision must be stated, not silent"


def test_a_large_file_no_longer_starves_later_files(tmp_path):
    """Previously the loop broke at exhaustion and later files vanished."""
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    _big_log(runtime / "aaa_serve_entry_verify.log", TAIL_MARKS)
    _big_log(runtime / "bbb_runtime_selfcheck.log", ["SECOND FILE TAIL"])
    (runtime / "ccc_startup_run1.log").write_text("startup ok\n", encoding="utf-8")
    (runtime / "ddd_restart_run2.log").write_text("restart ok\n", encoding="utf-8")
    listed = _by_path(_packet(tmp_path, _node(tmp_path)))
    for name in ("aaa_serve_entry_verify.log", "bbb_runtime_selfcheck.log",
                 "ccc_startup_run1.log", "ddd_restart_run2.log"):
        assert f"deliverables/runs/runtime/{name}" in listed, name


def test_every_listed_file_carries_bytes_and_sha256(tmp_path):
    """AC06-style "register bytes+sha256" becomes decidable without the content."""
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    target = runtime / "startup_run1.log"
    target.write_text("MAINT_READY port=8183\n", encoding="utf-8")
    entry = _by_path(_packet(tmp_path, _node(tmp_path)))["deliverables/runs/runtime/startup_run1.log"]
    assert entry["bytes"] == target.stat().st_size
    assert entry["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_a_starved_file_still_carries_its_hash(tmp_path):
    """Even when the excerpt is tiny, the code-computed facts are present."""
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    for i in range(6):
        _big_log(runtime / f"log_{i}.log", [f"TAIL-{i}"])
    listed = _by_path(_packet(tmp_path, _node(tmp_path)))
    assert len(listed) == 6
    for item in listed.values():
        assert item["bytes"] > 0 and len(item["sha256"]) == 64


# --- what must not change ----------------------------------------------------

def test_a_small_file_is_delivered_whole_and_unmarked(tmp_path):
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "launch_recipe.json").write_text('{"entry": "serve.py"}', encoding="utf-8")
    entry = _by_path(_packet(tmp_path, _node(tmp_path)))["deliverables/runs/runtime/launch_recipe.json"]
    assert entry["content"] == '{"entry": "serve.py"}'
    assert entry["truncated"] is False and entry["excerpt"] == "full"


def test_the_total_budget_is_not_raised(tmp_path):
    """The fix redistributes the budget; it must not spend more."""
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    for i in range(8):
        _big_log(runtime / f"log_{i}.log", [f"TAIL-{i}"])
    payload = _packet(tmp_path, _node(tmp_path))
    spent = sum(len(item.get("content", "")) for item in payload["actual_files"])
    assert spent <= 96000 + 8 * 200, spent  # + per-file elision markers


def test_binary_and_excluded_suffixes_are_still_skipped(tmp_path):
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "probe.db").write_bytes(b"SQLite format 3\x00")
    (runtime / "notes.txt").write_text("kept", encoding="utf-8")
    listed = _by_path(_packet(tmp_path, _node(tmp_path)))
    assert "deliverables/runs/runtime/notes.txt" in listed
    assert "deliverables/runs/runtime/probe.db" not in listed


def test_a_missing_file_does_not_abort_the_packet(tmp_path):
    runtime = tmp_path / "deliverables" / "runs" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "present.txt").write_text("here", encoding="utf-8")
    node = _node(tmp_path)
    node.metadata["expected_artifact_paths"] = ["deliverables/runs/runtime/gone.txt"]
    listed = _by_path(_packet(tmp_path, node))
    assert "deliverables/runs/runtime/present.txt" in listed
