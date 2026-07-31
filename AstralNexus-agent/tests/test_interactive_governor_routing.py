from __future__ import annotations

import json
from pathlib import Path

from AstralNexus.cli.main import _govern_interactive_turn
from AstralNexus.core.governor import (
    GlobalGovernor,
    InteractiveRouteDecision,
)
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.trace import TraceRecorder


class RouteClient:
    def __init__(self, payload: dict):
        self.payload = payload

    def chat(self, messages, **kwargs):
        assert kwargs["response_format"] == "json"
        assert kwargs["tools"] is None
        return json.dumps(self.payload, ensure_ascii=False)


def test_governor_keeps_small_question_in_direct_chat(tmp_path: Path) -> None:
    governor = GlobalGovernor(
        TaskGraph(),
        TraceRecorder(tmp_path),
        llm_client=RouteClient({
            "mode": "direct_chat",
            "complexity_score": 0.1,
            "parallel_benefit": 0.0,
            "required_capabilities": [],
            "estimated_nodes": 1,
            "needs_artifacts": False,
            "needs_verification": False,
            "reasoning": "simple question",
            "task_contract_summary": "answer a question",
        }),
    )

    decision = governor.decide_interactive_route("你好，你是谁？")

    assert decision.mode == "direct_chat"
    assert decision.estimated_nodes == 1


def test_governor_promotes_complex_delivery_to_graph(tmp_path: Path) -> None:
    governor = GlobalGovernor(
        TaskGraph(),
        TraceRecorder(tmp_path),
        llm_client=RouteClient({
            "mode": "direct_chat",
            "complexity_score": 0.62,
            "parallel_benefit": 0.7,
            "required_capabilities": ["backend", "frontend", "testing"],
            "estimated_nodes": 5,
            "needs_artifacts": True,
            "needs_verification": True,
            "reasoning": "multi-capability delivery",
            "task_contract_summary": "build and verify a full-stack service",
        }),
    )

    decision = governor.decide_interactive_route("实现并运行一个全栈工单平台")

    assert decision.mode == "governed_run"
    assert "governor_complexity_guard" in decision.reason_codes


def test_active_run_converts_new_governed_work_to_requirement_change(tmp_path: Path) -> None:
    governor = GlobalGovernor(
        TaskGraph(),
        TraceRecorder(tmp_path),
        llm_client=RouteClient({
            "mode": "governed_run",
            "complexity_score": 0.8,
            "parallel_benefit": 0.8,
            "required_capabilities": ["database", "testing"],
            "estimated_nodes": 4,
            "needs_artifacts": True,
            "needs_verification": True,
            "reasoning": "changes active delivery",
            "task_contract_summary": "migrate the active project database",
        }),
    )

    decision = governor.decide_interactive_route(
        "新增需求：数据库改为达梦。",
        active_run=True,
    )

    assert decision.mode == "requirement_change"
    assert "active_run_patch_required" in decision.reason_codes


class ControllerStub:
    def __init__(self, decision: InteractiveRouteDecision):
        self.decision = decision
        self.started = []
        self.injected = []

    def set_event_callback(self, callback):
        self.callback = callback

    def decide_route(self, text, history):
        return self.decision

    def start(self, task, *, output=None):
        self.started.append((task, output))
        return True

    def start_text(self, text, **kwargs):
        self.started.append((text, kwargs.get("output")))
        return True

    def inject_requirement_text(self, text):
        self.injected.append(text)


def test_interactive_boundary_starts_governed_run() -> None:
    controller = ControllerStub(InteractiveRouteDecision(
        mode="governed_run",
        complexity_score=0.8,
        parallel_benefit=0.7,
        required_capabilities=["coding", "testing"],
        estimated_nodes=4,
        needs_artifacts=True,
        needs_verification=True,
        reasoning="complex",
    ))
    history = []

    consumed = _govern_interactive_turn(
        "构建并测试一个服务",
        history,
        controller,
    )

    assert consumed is True
    assert controller.started
    assert history[-1]["role"] == "assistant"

