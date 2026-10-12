"""Single delivery lead; default gate is output delta plus present_files."""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Any
from dataclasses import asdict, fields
from AstralNexus.core.delivery import DeliveryManager
from AstralNexus.core.delivery_snapshot import DeliveryRejected, authorize_snapshot, snapshot, FRAMEWORK_FILES
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.presentation import Presentation, delivery_receipt, output_inventory, presentation_scope
from AstralNexus.runtime.goal_evaluator import GoalState, evaluate_goal


class FinalDeliveryPipeline:
    def __init__(self, runtime: Any, *, hooks: dict | None = None):
        self.runtime = runtime
        self.hooks = dict(hooks or {})
        self.before: dict = {}
        self.client: Any = None
        self.receipt: dict | None = None
        self.goal_state: GoalState | None = None
        self.presentation: Presentation | None = None
        self.store: ExecutionStore | None = None
        self.run_id = ""

    def begin(self, run_dir: Path) -> None:
        self.before = output_inventory(run_dir / "outputs")

    def _check_cancel(self) -> None:
        from AstralNexus.core.exceptions import InterruptionRequested
        if getattr(self.runtime, "cancel_requested", lambda: False)():
            raise InterruptionRequested("delivery cancelled")

    def _stream_turn(self, client: Any, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        from AstralNexus.runtime.llm_client import RuntimeNoProgressError
        try:
            return client.stream_chat(messages, **kwargs)
        except RuntimeNoProgressError as exc:
            # Direct Goal work has no graph Governor to receive this handoff.
            # Preserve the stop as structured control flow; never relabel it
            # as a host exception or silently start another worker.
            raise DeliveryRejected({"reason": "no_progress_breaker",
                "guard": exc.progress, "goal_active": bool(self.goal_state and self.goal_state.active)}) from exc

    def _client(self, model_config: dict | None):
        if self.client is not None:
            return self.client
        from AstralNexus.runtime.llm_client import LLMClient
        from AstralNexus.runtime.model_selector import get_model_config, DEFAULT_MODEL
        config = model_config or get_model_config()
        def event(item):
            self.runtime.record_cost("__delivery__", item.get("event_type", "llm.event"), item)
            self.runtime.trace_recorder.record(item.get("event_type", "llm.event"), item)
        self.client = LLMClient(model=config.get("model") or DEFAULT_MODEL, base_url=config.get("base_url"),
                                interactive=False, event_callback=event)
        return self.client

    @staticmethod
    def _copy_working_outputs(run_dir: Path, outputs: Path) -> None:
        # Single bridge for pre-existing worker layout. It copies bytes only;
        # source assertions never authorize or synthesize product files.
        working = run_dir / "deliverables"
        if not working.is_dir():
            return
        import re
        for child in working.iterdir():
            if child.name in FRAMEWORK_FILES or child.name in {"current", "current.json", "delivery_manifest.json"} or re.fullmatch(r"v\d+", child.name):
                continue
            if child.is_symlink():
                raise DeliveryRejected({"reason": "working_output_link"})
            rows = snapshot(child) if child.is_dir() else (("", "", 0),)
            for name, _, _ in rows:
                source = child / name if name else child
                target = outputs / child.name / name if name else outputs / child.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() == source.read_bytes():
                    continue
                shutil.copy2(source, target)

    def _visible_evidence(self, run_dir: Path, answer: str) -> dict:
        receipts = []
        store = ExecutionStore(run_dir)
        with store._connect() as db:
            execution_summary = {str(row[0]): int(row[1]) for row in db.execute(
                "SELECT json_extract(payload, '$.tool'), COUNT(*) FROM receipts "
                "WHERE json_extract(payload, '$.tool') IN ('terminal_exec','python_exec','pytest_run') "
                "GROUP BY json_extract(payload, '$.tool')")}
            # Keep separate bounded lanes: read-only Python probes must not
            # evict shell/test executions from the next repair handoff.
            for row in db.execute("""WITH ranked AS (
                SELECT payload, seq, json_extract(payload, '$.tool') AS tool,
                       ROW_NUMBER() OVER (PARTITION BY json_extract(payload, '$.tool') ORDER BY seq DESC) AS ordinal
                FROM receipts
            ) SELECT payload FROM ranked
              WHERE (tool IN ('terminal_exec','pytest_run','python_exec') AND ordinal <= 10)
                 OR (tool = 'file_write' AND ordinal <= 2)
              ORDER BY CASE tool WHEN 'pytest_run' THEN 0 WHEN 'terminal_exec' THEN 1
                                WHEN 'python_exec' THEN 2 ELSE 3 END, seq DESC"""):
                item = json.loads(row[0])
                raw = item.get("raw_result") or {}
                if not isinstance(raw, dict):
                    raw = {"content": str(raw)}
                arguments = item.get("arguments")
                if arguments is not None:
                    arguments = str(arguments)
                receipts.append({"receipt_id": item.get("receipt_id") or item.get("id") or item.get("call_id"),
                    "execution_id": item.get("execution_id"), "tool": item.get("tool"),
                    "arguments": arguments[:1200] if arguments else arguments,
                    "arguments_truncated": bool(arguments and len(arguments) > 1200), "status": item.get("status"),
                    "returncode": raw.get("returncode", item.get("returncode")), "stdout": str(raw.get("stdout") or item.get("stdout_tail") or "")[-600:],
                    "stderr": str(raw.get("stderr") or item.get("stderr_tail") or "")[-600:],
                    "content": str(raw.get("content") or "")[:600]})
        return {"assistant": answer[-4000:], "tools": receipts, "execution_receipt_counts": execution_summary,
                "outputs": output_inventory(run_dir / "outputs")}

    def _finalize_receipt(self) -> dict:
        if self.receipt is not None:
            return self.receipt
        if not self.runtime.owns_runtime():
            raise DeliveryRejected({"reason": "ownership_lost"})
        if self.presentation is None or self.store is None:
            raise RuntimeError("delivery context was not initialized")
        receipt = delivery_receipt(self.before, output_inventory(self.presentation.outputs), self.presentation.paths)
        receipt.update(run_id=self.run_id, outputs_root=str(self.presentation.outputs), gate_policy_version="presented-output-v1")
        try:
            receipt = self.store.record_delivery(self.run_id, receipt)
        except Exception as exc:
            raise DeliveryRejected({"reason": "delivery_receipt_persistence_failed", "error": str(exc)}) from exc
        self.receipt = receipt
        self.runtime.trace_recorder.record("run.delivery", receipt)
        return receipt

    def execute(self, **kwargs) -> dict[str, str]:
        from AstralNexus.runtime.execution_fence import execution_fence_scope
        provider = getattr(self.runtime, "execution_fence", lambda: None)()
        with execution_fence_scope(Path(kwargs["run_dir"]), provider):
            try:
                return self._execute(**kwargs)
            except BaseException:
                # Cancellation and goal failure still need a unique terminal fact.
                # An old/fenced owner must leave this to the new owner.
                if self.presentation is not None and self.receipt is None and self.runtime.owns_runtime():
                    self._finalize_receipt()
                raise

    def _execute(self, *, task_content: str, run_dir: Path, model_config: dict | None = None,
                 goal: str | None = None) -> dict[str, str]:
        run_dir = Path(run_dir)
        outputs = run_dir / "outputs"
        outputs.mkdir(exist_ok=True)
        from AstralNexus.runtime.run_context import get_env
        workspace = Path(get_env("ASTRALNEXUS_WORKSPACE", "") or run_dir)
        presentation = self.presentation = Presentation(run_dir, outputs, workspace=workspace)
        store = self.store = ExecutionStore(run_dir)
        run_id = self.run_id = str(getattr(self.runtime, "run_id", run_dir.name))
        saved_goal: dict[str, Any] = getattr(self.runtime, "goal_snapshot", lambda: {})()
        if goal and saved_goal.get("objective") == goal and saved_goal.get("active"):
            self.goal_state = GoalState(**{key: value for key, value in saved_goal.items() if key in {field.name for field in fields(GoalState)}})
        else:
            self.goal_state = GoalState(goal) if goal else None
        self._check_cancel()
        self._copy_working_outputs(run_dir, outputs)
        answer = ""
        client: Any = self._client(model_config) if output_inventory(outputs) or goal else None
        goal_client = client
        if goal and hasattr(client, "api_key"):
            from AstralNexus.runtime.llm_client import LLMClient
            from AstralNexus.runtime.run_context import get_env
            config = model_config or {}
            goal_client = LLMClient(model=config.get("goal_model") or config.get("sub_model") or get_env("ASTRALNEXUS_SUB_MODEL", "") or client.model,
                base_url=client.base_url, api_key=client.api_key, interactive=False, event_callback=client._event_callback)
        from AstralNexus.tools.assembly import assemble_tools, capability_guidance
        # Artifact-first has exactly one product writer: workers. Delivery lead
        # receives only present_files; explicit goal continuation is a new work
        # turn, not a second concurrent final synthesizer.
        with presentation_scope(presentation):
            if client is not None:
                bootstrap_goal = bool(goal and not output_inventory(outputs))
                lead_paths = ["outputs"] if bootstrap_goal else []
                lead_tools = assemble_tools() if bootstrap_goal else assemble_tools(candidates={"present_files"})
                execution = store.begin("goal-lead" if bootstrap_goal else "delivery-lead", {"owned_paths": lead_paths, "acceptance_criteria": []})
                client.runtime_execution_id = execution
                try:
                    answer = self._stream_turn(client, [
                        {"role": "system", "content": ("You are the sole work lead for an explicit goal. Use actual tools to complete the goal in outputs. Read back written content as visible evidence, then call present_files. Do not delegate presentation or treat tool names as files. Report real failures. Build and execute verification before writing test reports; no self-authored report is execution evidence. Write large files in small sequential chunks. " + capability_guidance(lead_tools) if bootstrap_goal else "You are the sole delivery lead. Product files are frozen. Do not rewrite them. Use present_files to show outputs to the user; a short explanation alone does not present files.")},
                        {"role": "user", "content": json.dumps({"task": task_content, "available_outputs": list(output_inventory(outputs)), "outputs_root": str(outputs.resolve())}, ensure_ascii=False)},
                    ], tools=lead_tools, workspace_root=run_dir, owned_paths=lead_paths, max_tool_rounds=None if bootstrap_goal else 4,
                       cancel_check=lambda: getattr(self.runtime, "cancel_requested", lambda: False)())
                    store.finish(execution, "succeeded", {"content": answer})
                except BaseException:
                    store.interrupt_open(execution)
                    raise
            while self.goal_state is not None:
                self._check_cancel()
                if not self.runtime.owns_runtime():
                    raise DeliveryRejected({"reason": "ownership_lost"})
                visible = self._visible_evidence(run_dir, answer)
                try:
                    verdict = evaluate_goal(goal_client, self.goal_state.objective, visible)
                except Exception as exc:
                    raise DeliveryRejected({"reason": "verification_unavailable", "error": str(exc), "goal_active": True}) from exc
                action = self.goal_state.decide(verdict, visible)
                record_goal = getattr(self.runtime, "record_goal_state", None)
                if record_goal is not None:
                    record_goal(asdict(self.goal_state))
                self.runtime.trace_recorder.record("goal.evaluated", {"verdict": verdict, "action": action, "continuations": self.goal_state.continuations})
                if action == "satisfied":
                    break
                if action != "continue":
                    raise DeliveryRejected({"reason": action, "goal_verdict": verdict, "goal_active": True})
                continuation_tools = assemble_tools()
                execution = store.begin("goal-continuation", {"owned_paths": ["outputs"], "acceptance_criteria": []})
                client.runtime_execution_id = execution
                try:
                    answer = self._stream_turn(client, [
                        {"role": "system", "content": "Continue the explicitly requested goal. Work sequentially in outputs only; use actual tools and present_files. Do not claim unobserved success. Preserve the execution evidence and confirmed diagnosis in visible_evidence. When an execution has failed with a concrete cause, inspect only the implicated code, make a targeted modification, then rerun the failed command. Do not spend the turn repeatedly rediscovering that same failure. Preserve assertions; never replace failures with success reports. " + capability_guidance(continuation_tools)},
                        {"role": "user", "content": json.dumps({"goal": goal, "blocker": verdict, "visible_evidence": visible}, ensure_ascii=False)},
                    ], tools=continuation_tools,
                       workspace_root=run_dir, owned_paths=["outputs"],
                       cancel_check=lambda: getattr(self.runtime, "cancel_requested", lambda: False)())
                    store.finish(execution, "succeeded", {"content": answer})
                except BaseException:
                    store.interrupt_open(execution)
                    raise
        self._check_cancel()
        receipt = self._finalize_receipt()
        if not receipt["satisfied"]:
            raise DeliveryRejected({"reason": "artifact_delivery_incomplete", "receipt": receipt})
        if not output_inventory(outputs):
            return {}
        manager = DeliveryManager(run_dir)
        from AstralNexus.core.output_paths import RunOutputLayout
        RunOutputLayout(run_dir).begin_delivery_draft()
        staging = manager.staging_dir
        for name, _, _ in snapshot(outputs):
            source, target = outputs / name, staging / "outputs" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        gate = {"overall_status": "pass", "delivery_authorized": True, "failed_tests": [], "unknown_tests": [],
                "gate_policy_version": "presented-output-v1", "task_version": 1, "delivery_receipt": receipt,
                "receipt_store_root": str(run_dir.resolve()), "publication_prefix": "outputs"}
        (staging / "delivery_verification.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / "delivery_summary.json").write_text(json.dumps({"delivery_gate": gate, "response": answer}, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / "acceptance_report.md").write_text("# 交付呈现结果\n\n本报告只证明本轮输出已呈现，不证明全部业务要求满足。\n\n- run.delivery: satisfied=true\n- Goal: " + ("satisfied" if goal else "未启用") + "\n", encoding="utf-8")
        authorization = authorize_snapshot(staging, {}, gate, ownership_identity=self.runtime.publication_fence())
        manifest = manager.publish(authorized=authorization, ownership_check=self.runtime.owns_runtime,
                                   fence_provider=self.runtime.publication_fence)
        version = run_dir / "deliverables" / ("v" + str(manifest["version"]))
        self._check_cancel()
        return {"summary_json": str(version / "delivery_summary.json"), "acceptance_report": str(version / "acceptance_report.md")}

    def stage(self, operation: str, **kwargs):
        if operation == "synthesize":
            return self.execute(**kwargs)
        if operation == "failure":
            return {}
        raise ValueError("Removed delivery operation: " + operation)
