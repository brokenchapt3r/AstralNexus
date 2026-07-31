"""Real-API verification for dynamic background candidate generation.

Usage:
    python scripts/verify_real_background_candidates.py --goal "..."

The API key is read from the environment and is never written to output.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from AstralNexus.core.orchestrator import Orchestrator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if not (os.environ.get("ASTRALNEXUS_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")):
        raise SystemExit("未配置 ASTRALNEXUS_API_KEY 或 DASHSCOPE_API_KEY，拒绝伪造离线验收")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.output or (Path.cwd() / f"real_api_background_candidates_{stamp}"))
    run_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = Orchestrator(run_dir, max_iterations=0, max_parallel=2)
    config = {"model": args.model} if args.model else None
    orchestrator.enable_real_llm_worker_matching(config)
    root_node_id = orchestrator.initialize_task_graph(args.goal)
    subtasks = orchestrator.plan_task_decomposition(root_node_id)
    candidates = []
    for node_id in subtasks:
        node = orchestrator.task_graph.get_node(node_id)
        if node is not None:
            candidates.extend(node.metadata.get("background_candidates", []))
    result = {
        "goal": args.goal,
        "model": args.model or "configured_model",
        "real_llm": True,
        "subtask_count": len(subtasks or []),
        "background_candidate_count": len(candidates),
        "background_candidates": candidates,
        "created_node_ids": subtasks,
        "trace": str(run_dir / "trace.jsonl"),
    }
    (run_dir / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
