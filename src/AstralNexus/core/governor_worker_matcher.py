"""Bounded Worker Matcher model interaction with typed failure categories."""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional


class WorkerMatcherService:
    def __init__(self, *, client, trace, enabled: bool, parser):
        self.llm_client = client
        self.trace_recorder = trace
        self.llm_matching_enabled = enabled
        self.parser = parser

    def match(
        self,
        task: Dict[str, Any],
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Ask the real LLM to choose among registered Worker Profiles.

        The model is a decision-maker, not a registry.  Its answer is
        accepted only when it names an existing profile; capability coverage,
        resource availability and contracts remain code-enforced below.
        """
        from AstralNexus.core.governance_types import EventType, WorkerMatcherTransientError, WorkerMatcherProfileError
        if not self.llm_matching_enabled or self.llm_client is None:
            return None
        catalogue = [
            {
                "worker_type": item.get("worker_type"),
                "capabilities": item.get("capabilities", []),
                "tools": item.get("tools", []),
                "model_preference": item.get("model_preference"),
                "resource_tier": item.get("resource_tier"),
                "input_contract": item.get("input_contract", ""),
                "output_contract": item.get("output_contract", ""),
                "reasoning_level": item.get("reasoning_level", "medium"),
            }
            for item in candidates
        ]
        prompt = json.dumps({
            "task": {
                "task_id": task.get("task_id") or task.get("node_id"),
                "goal": task.get("goal", ""),
                "required_capabilities": task.get("required_capabilities", []),
                "execution_target": task.get("execution_target") or task.get("resource_tier"),
                "risk_level": task.get("risk_level", "medium"),
            },
            "registered_worker_profiles": catalogue,
            "output_schema": {
                "worker_type": "existing profile name only",
                "reason_codes": "list of short reasons",
                "confidence": "number 0..1",
            },
        }, ensure_ascii=False)
        # Bounded retry for TRANSIENT faults only (malformed model output or
        # transport errors).  Live r13c 2026-09-29T11:35:29Z: a single
        # "worker matcher returned non-object" response propagated out of
        # select_worker_profile and terminally blocked healthy running node
        # N-122 before any attempt.  Hallucinated profile names and explicit
        # null decisions are not transient and never enter this retry.
        parsed: Any = None
        transient: Optional[Exception] = None
        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                text = self.llm_client.chat([
                    {
                        "role": "system",
                        "content": (
                            "你是天枢 AstralNexus Global Governor 的 Worker Matcher。"
                            "只能从已注册 Profile 中选择，不能虚构角色、模型或工具。"
                            "优先选择能力字段有明确匹配的 Profile；如果任务使用了目录中没有的领域能力词，"
                            "必须结合任务目标、输入输出契约、工具和 Profile 描述选择最接近且可安全承接的已注册 Profile，"
                            "并在 reason_codes 中标记 SEMANTIC_PROFILE_MATCH。只有所有已注册 Profile 都无法安全承接时，"
                            "才返回 worker_type=null，以便 Governor 补充能力或重规划。不得虚构能力覆盖，不得把猜测写成已验证事实。"
                            "只输出 JSON。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ])
                try:
                    candidate = self.parser(text)
                except (ValueError, TypeError) as exc:
                    raise WorkerMatcherTransientError("worker matcher returned malformed JSON") from exc
                if not isinstance(candidate, dict):
                    raise WorkerMatcherTransientError(
                        "worker matcher returned non-object"
                    )
                parsed = candidate
                break
            except Exception as exc:
                from AstralNexus.runtime.llm_client import is_transport_interruption
                if not (isinstance(exc, (WorkerMatcherTransientError, TimeoutError, ConnectionError)) or is_transport_interruption(exc)):
                    raise
                transient = exc
                self.trace_recorder.record(EventType.WORKER_MATCHER_FAILED, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "error": str(exc)[:500],
                    "reason": "transient_fault",
                    "attempt": attempt,
                    "attempts": attempts,
                })
        if parsed is None:
            raise WorkerMatcherTransientError(
                f"real LLM Worker Matcher failed after {attempts} attempts: "
                f"{str(transient)[:400]}"
            ) from transient
        try:
            worker_type = str(parsed.get("worker_type") or "").strip()
            if not worker_type:
                self.trace_recorder.record(EventType.WORKER_MATCHER_NO_MATCH, {
                    "task_id": task.get("task_id") or task.get("node_id"),
                    "reason_codes": parsed.get("reason_codes", []),
                })
                return {
                    "worker_type": None,
                    "reason_codes": [
                        str(x) for x in parsed.get("reason_codes", [])
                        if str(x).strip()
                    ],
                    "confidence": max(
                        0.0,
                        min(1.0, float(parsed.get("confidence", 0.0) or 0.0)),
                    ),
                }
            selected = next(
                (item for item in candidates if str(item.get("worker_type")) == worker_type),
                None,
            )
            if selected is None:
                raise WorkerMatcherProfileError(
                    f"unknown worker profile: {worker_type}"
                )
            decision = {
                "worker_type": worker_type,
                "reason_codes": [str(x) for x in parsed.get("reason_codes", []) if str(x).strip()],
                "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0))),
            }
            self.trace_recorder.record(EventType.WORKER_MATCHER_DECIDED, {
                "task_id": task.get("task_id") or task.get("node_id"),
                "decision": decision,
            })
            return decision
        except (WorkerMatcherProfileError, ValueError, TypeError) as exc:
            self.trace_recorder.record(EventType.WORKER_MATCHER_FAILED, {
                "task_id": task.get("task_id") or task.get("node_id"),
                "error": str(exc)[:500],
                "reason": "governance_violation",
            })
            if isinstance(exc, WorkerMatcherProfileError):
                raise WorkerMatcherProfileError(f"real LLM Worker Matcher failed: {str(exc)[:500]}") from exc
            raise WorkerMatcherProfileError(f"real LLM Worker Matcher failed: {str(exc)[:500]}") from exc

