"""Run every task contract as an independent, real-LLM acceptance process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--model", default="qwen-flash")
    parser.add_argument("--provider", default="dashscope")
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument(
        "--skip-task",
        action="append",
        default=[],
        help="Skip a task directory by exact name; may be supplied more than once",
    )
    parser.add_argument(
        "--allow-tools",
        action="store_true",
        help="Explicitly approve file writes and terminal execution for acceptance only",
    )
    args = parser.parse_args()

    tasks_root = Path(args.tasks_root).resolve()
    output_root = Path(args.output_root).resolve()
    source_root = Path(__file__).resolve().parents[2]
    output_root.mkdir(parents=True, exist_ok=True)
    status_path = output_root / "batch_status.json"
    records: list[dict] = []

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(source_root), existing_pythonpath) if item
    )
    if args.allow_tools:
        env["ASTRALNEXUS_ALLOW_FILE_WRITE"] = "true"
        env["ASTRALNEXUS_ALLOW_TERMINAL"] = "true"

    skipped = set(args.skip_task)
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        if task_dir.name in skipped:
            continue
        task_files = sorted(task_dir.glob("*.txt"))
        if not task_files:
            continue
        task_file = task_files[0]
        output_dir = output_root / task_dir.name
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "cli.stdout.log"
        stderr_path = output_dir / "cli.stderr.log"
        record = {
            "task": task_dir.name,
            "task_file": str(task_file),
            "output_dir": str(output_dir),
            "status": "running",
            "started_at": _stamp(),
        }
        records.append(record)
        status_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "AstralNexus.cli.main",
            "run",
            "--task",
            str(task_file),
            "--model",
            args.model,
            "--provider",
            args.provider,
            "--max-parallel",
            str(max(1, args.max_parallel)),
            "--output",
            str(output_dir),
        ]
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=task_dir,
                env=env,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
        record.update(
            {
                "status": "completed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "ended_at": _stamp(),
                "deliverables": sorted(
                    path.name
                    for path in (output_dir / "deliverables").glob("*")
                    if path.is_file()
                )
                if (output_dir / "deliverables").exists()
                else [],
            }
        )
        status_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if all(item["status"] == "completed" for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
