"""Fix V / Fix W：被取消过的运行必须还能恢复，且停机原因不得冒充用户操作。

实测来源（doc 18, r13b）：用户在 03:26 停止运行，`cancel_requested=1` 落入
runs.sqlite 且从未被清除。此后每次恢复都被 acquire() 正常接纳（`user_interrupt`
在 AUTO_RECOVERABLE_STOP_CODES 内），却在第一次租约心跳（lease_seconds//3）被
should_stop() 里这枚陈旧标志杀掉；停机又被记成 "user_interrupt"，把标志重新武装
起来——一个自我延续的陷阱。generation 2 跑满 36 分钟，generation 3 起每次 33 秒
即死，全程无人触碰键盘。
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta, timezone

from AstralNexus.cli.main import _start_lease_heartbeat
from AstralNexus.runtime.run_coordinator import RunCoordinator


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _coord(tmp_path, clock, run_id="run-1"):
    return RunCoordinator(
        tmp_path, run_id=run_id, lease_seconds=120, grace_seconds=10, now_fn=clock,
    )


# ---------------------------------------------------------------- Fix V

def test_cancelled_run_can_be_resumed_and_keeps_running(tmp_path):
    """接管必须清除 cancel_requested，否则新 owner 一出生就被判停。"""
    clock = Clock()
    coordinator = _coord(tmp_path, clock)
    lease = coordinator.acquire("owner-A")
    assert lease is not None

    assert coordinator.request_cancel() == "requested"
    assert coordinator.is_cancel_requested() is True
    # 旧 owner 仍在跑时，取消请求必须生效——这条不能被 Fix V 削弱。
    assert coordinator.should_stop(lease) is True

    coordinator.finish(lease, status="blocked", stop_reason="blocked",
                       stop_code="user_interrupt")

    clock.advance(600)
    resumed = coordinator.acquire("owner-B")
    assert resumed is not None, "user_interrupt 属于自动可恢复停机码"
    assert coordinator.is_cancel_requested() is False, "陈旧取消标志必须随接管清除"
    # 关键断言：新 owner 的第一次心跳不得把自己判停。
    assert coordinator.should_stop(resumed) is False


def test_resume_survives_repeated_cancel_interrupt_cycles(tmp_path):
    """复现自我延续的陷阱：三代都必须能真正继续，而不是各活一个心跳。"""
    clock = Clock()
    coordinator = _coord(tmp_path, clock)
    lease = coordinator.acquire("owner-1")
    for generation in range(2, 5):
        coordinator.request_cancel()
        coordinator.finish(lease, status="blocked", stop_reason="blocked",
                           stop_code="user_interrupt")
        clock.advance(600)
        lease = coordinator.acquire(f"owner-{generation}")
        assert lease is not None, f"generation {generation} 应可接管"
        assert coordinator.should_stop(lease) is False, (
            f"generation {generation} 在第一次心跳就被陈旧取消标志杀掉"
        )


def test_user_pause_still_blocks_automatic_resume(tmp_path):
    """Fix V 不得削弱更强的 paused_by_user 门槛。"""
    clock = Clock()
    coordinator = _coord(tmp_path, clock)
    lease = coordinator.acquire("owner-A")
    coordinator.request_pause()
    coordinator.finish(lease, status="blocked", stop_reason="paused",
                       stop_code="user_interrupt")
    clock.advance(600)
    assert coordinator.acquire("monitor") is None, "自动监控不得越过用户暂停"
    assert coordinator.acquire("operator", user_resume=True) is not None


def test_live_cancel_during_run_still_stops_the_current_owner(tmp_path):
    """取消对当前这一代仍然必须立刻生效。"""
    clock = Clock()
    coordinator = _coord(tmp_path, clock)
    lease = coordinator.acquire("owner-A")
    assert coordinator.should_stop(lease) is False
    coordinator.request_cancel()
    assert coordinator.should_stop(lease) is True


# ---------------------------------------------------------------- Fix W

class _FakeCoordinator:
    """最小心跳桩：lease_seconds 取 15 使 interval 落到下限 5 秒。"""

    lease_seconds = 15

    def __init__(self, renew_result="same", stop=False):
        self.renew_result = renew_result
        self.stop = stop

    def renew(self, lease):
        return None if self.renew_result is None else lease

    def should_stop(self, lease):
        return self.stop


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def test_lease_loss_reports_lease_lost_not_user_interrupt():
    event = threading.Event()
    pending: queue.Queue = queue.Queue()
    coordinator = _FakeCoordinator(renew_result=None)
    _holder, stop, thread, ownership_lost = _start_lease_heartbeat(
        coordinator, object(), interrupt_event=event, interrupt_queue=pending,
    )
    thread.join(timeout=20)
    stop.set()
    assert ownership_lost.is_set() and event.is_set()
    messages = [item.get("message") for item in _drain(pending)]
    assert messages == ["lease_lost"]
    assert "user_interrupt" not in messages, "租约丢失不得冒充用户中断"


def test_coordinator_stop_reports_its_own_reason():
    event = threading.Event()
    pending: queue.Queue = queue.Queue()
    coordinator = _FakeCoordinator(stop=True)
    _holder, stop, thread, ownership_lost = _start_lease_heartbeat(
        coordinator, object(), interrupt_event=event, interrupt_queue=pending,
    )
    thread.join(timeout=20)
    stop.set()
    assert ownership_lost.is_set() and event.is_set()
    assert [i.get("message") for i in _drain(pending)] == ["coordinator_stop_requested"]


def test_heartbeat_without_queue_keeps_old_behaviour():
    """队列缺席时只置位事件，不得抛异常（向后兼容）。"""
    event = threading.Event()
    coordinator = _FakeCoordinator(renew_result=None)
    _holder, stop, thread, ownership_lost = _start_lease_heartbeat(
        coordinator, object(), interrupt_event=event,
    )
    thread.join(timeout=20)
    stop.set()
    assert ownership_lost.is_set() and event.is_set()
