"""P1 (DeerFlow capped-semantics port): objective workspace delta booking.

Live r13c evidence: three wall-clock deaths on one contract were booked as
the same undifferentiated failure — N-123 attempt 2 had landed five
regenerated evidence files (90% of the deliverable) while N-123 attempt 1
and N-126 landed nothing at all. Recovery routing could not tell "continue
from the landed bytes" apart from "change strategy". These tests pin the
classifier that makes the two outcomes structurally different records.
"""
import hashlib

from AstralNexus.runtime.workspace_delta import (
    CLASSIFICATION_CAPPED_WITH_OUTPUT,
    CLASSIFICATION_CAPPED_ZERO_OUTPUT,
    CLASSIFICATION_WITH_OUTPUT,
    CLASSIFICATION_ZERO_OUTPUT,
    build_workspace_delta,
)
from AstralNexus.runtime.execution_store import snapshot_files


def _inv(**items):
    return {
        path: {
            "bytes": size,
            "sha256": hashlib.sha256(f"{path}:{size}".encode()).hexdigest(),
        }
        for path, size in items.items()
    }


def test_capped_with_output_when_owned_bytes_landed_before_the_cap():
    before = _inv()
    after = _inv(**{"deliverables/app/backend/app.py": 58_241})
    delta = build_workspace_delta(
        before, after, exhausted_reason="max_seconds", node_id="N-123",
    )
    assert delta["classification"] == CLASSIFICATION_CAPPED_WITH_OUTPUT
    assert delta["exhausted_reason"] == "max_seconds"
    assert delta["counts"] == {"added": 1, "removed": 0, "changed": 0}
    entry = delta["entries"][0]
    assert entry["path"] == "deliverables/app/backend/app.py"
    assert entry["kind"] == "added"
    assert entry["after_bytes"] == 58_241
    assert "before_bytes" not in entry


def test_capped_zero_output_when_the_wall_hit_with_nothing_landed():
    before = _inv(**{"collector.py": 100_025})
    after = _inv(**{"collector.py": 100_025})
    delta = build_workspace_delta(
        before, after, exhausted_reason="max_seconds", node_id="N-126",
    )
    assert delta["classification"] == CLASSIFICATION_CAPPED_ZERO_OUTPUT
    assert delta["counts"] == {"added": 0, "removed": 0, "changed": 0}
    assert delta["entries"] == []
    assert delta["entries_truncated"] is False


def test_modified_and_removed_kinds_carry_before_and_after_evidence():
    before = _inv(**{"a.py": 100, "b.py": 200})
    after = _inv(**{"a.py": 130})
    delta = build_workspace_delta(before, after, exhausted_reason=None)
    assert delta["classification"] == CLASSIFICATION_WITH_OUTPUT
    assert delta["counts"] == {"added": 0, "removed": 1, "changed": 1}
    by_path = {entry["path"]: entry for entry in delta["entries"]}
    assert by_path["a.py"]["kind"] == "changed"
    assert by_path["a.py"]["before_bytes"] == 100
    assert by_path["a.py"]["after_bytes"] == 130
    assert by_path["a.py"]["before_sha256"] != by_path["a.py"]["after_sha256"]
    assert by_path["b.py"]["kind"] == "removed"
    assert "after_bytes" not in by_path["b.py"]


def test_uncapped_zero_change_is_zero_output_not_capped():
    delta = build_workspace_delta(_inv(**{"a.py": 1}), _inv(**{"a.py": 1}))
    assert delta["classification"] == CLASSIFICATION_ZERO_OUTPUT
    assert delta["exhausted_reason"] is None


def test_none_snapshots_degrade_to_empty_delta():
    delta = build_workspace_delta(None, None, exhausted_reason="max_tool_calls")
    assert delta["classification"] == CLASSIFICATION_CAPPED_ZERO_OUTPUT
    assert delta["counts"] == {"added": 0, "removed": 0, "changed": 0}


def test_detail_limit_bounds_entries_and_flags_truncation():
    before = _inv()
    after = _inv(**{f"f{i:02d}.txt": 10 for i in range(50)})
    delta = build_workspace_delta(before, after, detail_limit=8)
    assert len(delta["entries"]) == 8
    assert delta["entries_truncated"] is True
    assert delta["counts"]["added"] == 50


def test_delta_is_json_serializable_and_node_scoped():
    import json
    delta = build_workspace_delta(
        _inv(), _inv(**{"x.txt": 5}), exhausted_reason="max_seconds", node_id="N-9",
    )
    assert delta["node_id"] == "N-9"
    assert json.loads(json.dumps(delta)) == delta


def test_snapshot_files_excludes_externalized_tool_outputs(tmp_path):
    """P4 interplay: the framework's externalization directory must never
    enter owned-file snapshots, or every staleness gate and workspace delta
    would see the runtime's own scratch bytes as worker writes."""
    out_dir = tmp_path / ".astralnexus_tool_outputs"
    out_dir.mkdir()
    (out_dir / "r0001_call_abc.json").write_text("{}", encoding="utf-8")
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    inventory = snapshot_files(tmp_path, [".astralnexus_tool_outputs", "real.py"])
    assert list(inventory) == ["real.py"]
