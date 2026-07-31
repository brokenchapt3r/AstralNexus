from __future__ import annotations

import time

from AstralNexus.cli.progress_reporter import RunProgressReporter


def test_progress_reporter_tracks_parallel_nodes_and_next_step(tmp_path):
    events = []
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        sink=events.append,
        interval_seconds=60,
    )
    try:
        reporter.handle_event({
            "event_type": "task_graph.snapshot",
            "nodes": [
                {
                    "node_id": "N-WAIT",
                    "role": "synthesizer",
                    "goal": "package delivery",
                    "status": "waiting_dependencies",
                }
            ],
        })
        reporter.handle_event({
            "event_type": "ready_wave.concurrency_decided",
            "wave_id": "wave-2",
            "node_count": 2,
            "selected_parallel": 2,
        })
        for node_id, worker, model in (
            ("N-ARCH", "Architect", "qwen3.7-max"),
            ("N-TEST", "Testing", "qwen-plus"),
        ):
            reporter.handle_event({
                "event_type": "task.execution.started",
                "node_id": node_id,
                "role": worker.lower(),
                "worker_type": worker,
                "selected_model": model,
                "goal": f"execute {node_id}",
            })

        report = reporter.build_report()
        assert "运行 2" in report
        assert "N-ARCH [Architect / qwen3.7-max]" in report
        assert "N-TEST [Testing / qwen-plus]" in report
        assert "N-WAIT[synthesizer]=waiting_dependencies" in report
        assert "不阻塞的同波节点继续并行" in report
        assert any(
            event.get("event_type") == "runtime.progress.report"
            and "并发宽度 2" in event.get("text", "")
            for event in events
        )
    finally:
        reporter.close()


def test_progress_reporter_reports_node_result_verification_and_completion(tmp_path):
    events = []
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        sink=events.append,
        interval_seconds=60,
    )
    try:
        reporter.handle_event({
            "event_type": "task.execution.started",
            "node_id": "N-01",
            "role": "coder",
            "worker_type": "Coding",
            "selected_model": "qwen3-coder-plus",
            "goal": "build application",
        })
        reporter.handle_event({
            "event_type": "node.result_submitted",
            "node_id": "N-01",
            "role": "coder",
            "summary": "created source files and test evidence",
        })
        reporter.handle_event({
            "event_type": "verifier.verified",
            "node_id": "N-01",
            "verdict": "approved",
            "overall_score": 0.91,
        })
        reporter.handle_event({
            "event_type": "node.completed",
            "node_id": "N-01",
            "status": "completed",
            "verification_score": 0.91,
        })

        progress_text = "\n".join(
            event.get("text", "")
            for event in events
            if event.get("event_type") == "runtime.progress.report"
        )
        assert "已提交结果，进入验证" in progress_text
        assert "Verifier 判定 approved" in progress_text
        assert "已完成；验证分 0.91" in progress_text
        assert "唤醒依赖该产物的节点" in progress_text
    finally:
        reporter.close()


def test_progress_reporter_does_not_create_fake_node_from_governor_llm(tmp_path):
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        interval_seconds=60,
    )
    try:
        reporter.handle_event({
            "event_type": "llm.request",
            "node_id": "T-005",
            "reason": "governor planning",
        })
        assert reporter.nodes == {}
    finally:
        reporter.close()


def test_progress_reporter_emits_periodic_heartbeat(tmp_path):
    events = []
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        sink=events.append,
        interval_seconds=1,
    )
    try:
        reporter.handle_event({
            "event_type": "task.execution.started",
            "node_id": "N-LONG",
            "role": "researcher",
            "worker_type": "Research",
            "selected_model": "qwen-flash",
            "goal": "collect evidence",
        })
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if any(event.get("kind") == "heartbeat" for event in events):
                break
            time.sleep(0.05)
        heartbeats = [event for event in events if event.get("kind") == "heartbeat"]
        assert heartbeats
        assert "N-LONG [Research / qwen-flash]" in heartbeats[-1]["text"]
        assert "接下来：" in heartbeats[-1]["text"]
    finally:
        reporter.close()


def test_progress_reporter_reports_provider_wait_without_hiding_node(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv("ASTRALNEXUS_WAIT_NOTICE_SECONDS", "1")
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        sink=events.append,
        interval_seconds=60,
    )
    try:
        reporter.handle_event({
            "event_type": "task.execution.started",
            "node_id": "N-SLOW",
            "worker_type": "Architect",
            "selected_model": "qwen3.7-max",
            "goal": "design architecture",
        })
        reporter.handle_event({
            "event_type": "llm.request",
            "node_id": "N-SLOW",
            "model": "qwen3.7-max",
        })
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if any(event.get("kind") == "provider_wait" for event in events):
                break
            time.sleep(0.05)
        notices = [event for event in events if event.get("kind") == "provider_wait"]
        assert notices
        assert "N-SLOW" in notices[-1]["text"]
        assert "qwen3.7-max" in notices[-1]["text"]
        assert "进程仍在运行" in notices[-1]["text"]
    finally:
        reporter.close()


def test_progress_reporter_keeps_plain_cli_run_notices(tmp_path, capsys):
    reporter = RunProgressReporter(
        run_dir=str(tmp_path),
        interval_seconds=60,
    )
    try:
        reporter.handle_event({
            "event_type": "interactive.run.progress",
            "text": "开始执行任务: build MVP",
        })
        assert "开始执行任务: build MVP" in capsys.readouterr().out
    finally:
        reporter.close()


def test_progress_reporter_final_summary_has_delivery_metrics(tmp_path):
    reporter = RunProgressReporter(run_dir=str(tmp_path), interval_seconds=60)
    try:
        reporter.handle_event({
            "event_type": "task.execution.started",
            "node_id": "N-01",
            "role": "coder",
        })
        reporter.handle_event({
            "event_type": "node.completed",
            "node_id": "N-01",
            "status": "completed",
        })
        reporter.handle_event({
            "event_type": "recovery.triggered",
            "node_id": "N-02",
            "summary": "retry provider",
        })
        reporter.handle_event({
            "event_type": "artifact.created",
            "details": {"path": str(tmp_path / "report.md")},
        })
        report = reporter.build_report(final=True)
        assert "总节点数：1" in report
        assert "成功节点：1" in report
        assert "失败节点：0" in report
        assert "恢复次数：1" in report
        assert f"输出目录：{tmp_path}" in report
        assert "report.md" in report
    finally:
        reporter.close()
