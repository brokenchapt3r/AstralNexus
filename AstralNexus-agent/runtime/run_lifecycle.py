"""Bounded lease heartbeat shutdown and stale callback suppression."""
from __future__ import annotations
import logging
import threading
logger = logging.getLogger(__name__)


def _start_lease_heartbeat(coordinator, lease, interrupt_event=None, interrupt_queue=None):
    """每 lease_seconds/3 续租；失去所有权或收到取消/暂停请求时通过
    interrupt_event 协作式终止执行（进程死亡则由租约过期兜底接管）。

    Fix W (doc 18, 2026-09-15 r13b): 心跳此前只置位 interrupt_event，不留原因，
    于是 _mark_interrupted 落到 ``self._interrupt_reason or "user_interrupt"``
    的兜底分支，把「租约丢失/协调器要求停机」记成「用户中断」。实测代价：
    r13b 连续三次停机都写着 user_interrupt，而没有任何人碰过键盘，取证因此
    指向了不存在的操作者。现在把真实原因放进 interrupt_queue，由
    _process_pending_interrupts 收敛成 _interrupt_reason；队列缺席时行为不变。
    """
    holder = {"lease": lease}
    stop = threading.Event()
    ownership_lost = threading.Event()
    interval = max(5, coordinator.lease_seconds // 3)

    def _signal(reason: str):
        if stop.is_set():
            return
        ownership_lost.set()
        if interrupt_queue is not None:
            try:
                interrupt_queue.put({"type": "interrupt", "message": reason})
            except Exception:  # 队列故障不得掩盖停机本身
                pass
        if interrupt_event is not None:
            interrupt_event.set()

    def _loop():
        while not stop.wait(interval):
            renewed = coordinator.renew(holder["lease"])
            if stop.is_set():
                return
            if renewed is None:
                _signal("lease_lost")
                return
            holder["lease"] = renewed
            if coordinator.should_stop(renewed):
                _signal("coordinator_stop_requested")
                return

    thread = threading.Thread(target=_loop, name="run-lease-heartbeat", daemon=True)
    thread.start()
    return holder, stop, thread, ownership_lost


def _stop_lease_heartbeat(stop, thread, timeout: float = 5.0) -> None:
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("lease heartbeat did not finish before cleanup deadline")
