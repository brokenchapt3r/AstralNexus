"""Exercise real tool-result externalization without touching caller-owned files."""
from __future__ import annotations
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from AstralNexus.runtime.tool_output_budget import externalize_tool_result


def _main(size: int, workspace: Path) -> dict:
    raw = {"status": "success", "content": "x" * size}
    view, event = externalize_tool_result(raw, workspace_root=workspace, call_id="probe", tool_round=1, cap_chars=12000)
    restored = json.loads((workspace / view["full_output_ref"]).read_text(encoding="utf-8")) if event else view
    return {"verdict": {"raw_content_is_full": restored == raw,
                        "wrapped_content_stays_small": len(json.dumps(view)) <= 12000}}


def run_probe(size: int, outputs_dir: Path, tmp_dir: Path) -> dict:
    if size < 0:
        raise ValueError("size must be non-negative")
    outputs_dir = Path(outputs_dir).resolve()
    tmp_dir = Path(tmp_dir).resolve()
    outputs_dir.mkdir(parents=True, exist_ok=True)
    created_scratch = not tmp_dir.exists()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as stack:
            workspace = Path(stack.enter_context(TemporaryDirectory(prefix="probe-run-", dir=outputs_dir)))
            stack.enter_context(TemporaryDirectory(prefix="probe-tmp-", dir=tmp_dir))
            return _main(size, workspace)
    finally:
        if created_scratch:
            # Only an empty directory created by this invocation may disappear.
            tmp_dir.rmdir()
