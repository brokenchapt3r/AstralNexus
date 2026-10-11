"""Entry-independent execution followed by mandatory delivery verification."""
from __future__ import annotations
import hashlib
import json
import time
import re
from pathlib import Path
from typing import Any
from dataclasses import asdict
from AstralNexus.core.delivery_pipeline import FinalDeliveryPipeline
from AstralNexus.core.delivery_ports import adapt_delivery_runtime


from AstralNexus.core.publication_validation import inspect_publication
from AstralNexus.core.delivery_snapshot import DeliveryRejected


class RunApplicationService:
    """Library, CLI and host callers share the same product completion rule."""
    def __init__(self, engine: Any, *, pipeline: Any = None):
        self._engine = engine
        self._pipeline = pipeline or FinalDeliveryPipeline(adapt_delivery_runtime(engine))

    def execute(self, *, task_content: str, run_dir: Path, constraints: list[str] | None = None,
                model_config: dict | None = None) -> dict[str, Any]:
        goal = task_content.strip() or "Execute task"
        from AstralNexus.core.exceptions import PlanningRejectedError
        if hasattr(self._pipeline, "begin"):
            self._pipeline.begin(run_dir)
        explicit_goal = task_content[6:].strip() if task_content.startswith("/goal ") else None
        task_content = explicit_goal or task_content
        if explicit_goal:
            started = time.monotonic()
            result = self.complete(execution={"status": "not_run", "mode": "direct_goal",
                "iterations": 0, "completed_tasks": 0, "failed_tasks": 0},
                task_content=task_content, run_dir=run_dir, model_config=model_config, goal=explicit_goal)
            result["elapsed_seconds"] = time.monotonic() - started
            return result
        try:
            execution = dict(self._engine.run(goal=task_content.strip() or goal, constraints=list(constraints or [])))
        except PlanningRejectedError as exc:
            return {"status": "failed", "execution_status": "failed", "stop_code": "planning_failed",
                    "stop_reason": str(exc), "delivery_status": "not_run", "delivery_authorized": False}

        if execution.get("status") != "completed":
            return {"status": execution.get("status", "failed"), "execution": execution,
                    "execution_status": execution.get("status"), "delivery_status": "not_run", "delivery_authorized": False}
        return self.complete(execution=execution, task_content=task_content, run_dir=run_dir, model_config=model_config, goal=explicit_goal)

    def _state(self, payload: dict) -> None:
        record = getattr(self._engine, "record_application_state", None)
        if record is not None and not record(payload):
            raise RuntimeError("product checkpoint ownership lost")

    def complete(self, *, execution: dict, task_content: str, run_dir: Path, model_config: dict | None = None,
                 goal: str | None = None) -> dict[str, Any]:
        goal = goal or (task_content[6:].strip() if task_content.startswith("/goal ") else None)
        direct_goal = bool(goal and execution.get("mode") == "direct_goal")
        if execution.get("status") != "completed" and not direct_goal:
            raise ValueError("cannot deliver an incomplete execution")
        initial_goal: dict[str, Any] = getattr(getattr(self._pipeline, "runtime", None), "goal_snapshot", lambda: {})()
        if not goal or initial_goal.get("objective") != goal or not initial_goal.get("active"):
            initial_goal = {"objective": goal, "active": True} if goal else {}
        self._state({"execution_status": "running" if direct_goal else "completed", "delivery_status": "running", "publication_status": "not_published", "run_status": "running", "phase": "executing", "stop_code": "",
                     "goal_state": initial_goal})
        try:
            if goal:
                artifacts = self._pipeline.execute(task_content=task_content, run_dir=run_dir, model_config=model_config, goal=goal)
            else:
                artifacts = self._pipeline.execute(task_content=task_content, run_dir=run_dir, model_config=model_config)
        except DeliveryRejected as exc:
            goal_state = getattr(self._pipeline, "goal_state", None)
            guard_stopped = exc.report.get("reason") == "no_progress_breaker"
            manual = guard_stopped or (goal_state is not None and exc.report.get("reason") in {"missing_evidence", "needs_user_input", "external_wait", "goal_no_progress", "goal_continuation_limit", "verification_unavailable"})
            result = {"status": "blocked" if manual else "delivery_failed", "execution": dict(execution), "execution_status": "blocked" if direct_goal and guard_stopped else "completed",
                      "delivery_status": "passed" if (getattr(self._pipeline, "receipt", None) or {}).get("satisfied") else "failed", "publication_status": "not_published", "delivery_authorized": False,
                      "stop_code": str(exc.report.get("reason") or "delivery_failed"), "delivery_verdict": exc.report, "artifacts": {}}
            self._state({**result, "run_status": "blocked" if manual else "failed", "delivery_receipt": getattr(self._pipeline, "receipt", None),
                         "goal_state": asdict(goal_state) if goal_state is not None else {}})
            return result
        except Exception as exc:
            runtime = getattr(self._pipeline, "runtime", None)
            if runtime is not None and not runtime.owns_runtime():
                raise
            cancelled = runtime is not None and runtime.cancel_requested()
            goal_state = getattr(self._pipeline, "goal_state", None)
            receipt = getattr(self._pipeline, "receipt", None)
            published = False
            if receipt is not None:
                try:
                    pointer = json.loads((Path(run_dir) / "deliverables/current.json").read_text(encoding="utf-8"))
                    version = str(pointer.get("path") or "")
                    if re.fullmatch(r"v[0-9]+", version):
                        summary = Path(run_dir) / "deliverables" / version / "delivery_summary.json"
                        packet = json.loads(summary.read_text(encoding="utf-8"))
                        if packet.get("delivery_gate", {}).get("delivery_receipt") == receipt:
                            published = bool(inspect_publication(run_dir, {"summary_json": str(summary),
                                "acceptance_report": str(summary.parent / "acceptance_report.md")})["complete"])
                except (OSError, ValueError, KeyError):
                    pass
            partial = {"status": "interrupted" if cancelled else "failed", "execution": dict(execution),
                       "execution_status": "interrupted" if direct_goal else "completed",
                       "delivery_status": "passed" if (receipt or {}).get("satisfied") else ("cancelled" if cancelled else "failed"),
                       "publication_status": "published" if published else "not_published",
                       "delivery_authorized": False, "artifacts": {},
                       "stop_code": "user_interrupt" if cancelled else "host_exception", "stop_reason": str(exc)}
            self._state({**partial, "run_status": partial["status"], "phase": partial["status"],
                         "delivery_receipt": receipt,
                         "goal_state": asdict(goal_state) if goal_state else ({"objective": goal, "active": True} if goal else {})})
            if cancelled:
                return partial
            raise
        receipt = getattr(self._pipeline, "receipt", None)
        if receipt is not None and not artifacts:
            verdict = {"published": False, "complete": bool(receipt.get("satisfied")), "authorized": True}
        else:
            verdict = inspect_publication(run_dir, artifacts)
        complete = verdict["complete"]
        goal_state = getattr(self._pipeline, "goal_state", None)
        if goal_state is not None and complete:
            goal_state.active = False
        result = {"status": "completed" if complete else "delivery_failed", "execution": dict(execution),
                  "execution_status": "completed", "delivery_status": "passed" if complete else "failed",
                  "publication_status": "published" if verdict["published"] else "not_published",
                  "delivery_authorized": verdict["authorized"], "publication": verdict, "artifacts": artifacts,
                  "completion_scope": "goal_and_presentation" if goal else "execution_and_presentation"}
        try:
            self._state({**result, "run_status": "completed" if complete else "failed", "phase": "completed", "delivery_receipt": receipt,
                         "goal_state": asdict(goal_state) if goal_state else {}})
        except BaseException:
            if goal_state is not None:
                goal_state.active = True
            raise
        return result
