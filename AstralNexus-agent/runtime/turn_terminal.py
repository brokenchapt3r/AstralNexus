"""Terminal stop reasons for a workbench chat turn.

Port of the ``runtime/runs/manager.py`` stop-reason model from DeerFlow: a
terminal record must carry *why* it ended, and the vocabulary lives in exactly
one place so a reader, an event payload and a test all name the same thing.

DeerFlow keeps ``ORPHAN_RECOVERY_STOP_REASON`` / ``STARTUP_ORPHAN_RECOVERY_ERROR``
as module constants with human-readable text; this module is the same shape for
our turn rows.  ``chat_turns`` gained ``stop_reason`` / ``error`` /
``goal_verdict`` because the row previously recorded only the status word: live
2026-10-07 the host died of page-file exhaustion, the run went ``interrupted``,
and the durable turn row still said ``running`` with no way to say why -- the
host API and the DB together could not explain that the run had ended.

Every reason below must be *decidable at the moment the terminal write happens*.
Nothing here may be inferred later from the trace.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# The vocabulary.  One source; tests and event payloads render these strings.
# ---------------------------------------------------------------------------

USER_CANCELLED: Final = "user_cancelled"
HOST_SHUTDOWN: Final = "host_shutdown"
WORKER_DISCONNECTED: Final = "worker_disconnected"
ORPHAN_RECOVERED: Final = "orphan_recovered"
HOST_EXCEPTION: Final = "host_exception"
WORKER_FAILED: Final = "worker_failed"

STOP_REASON_DESCRIPTIONS: Final[dict[str, str]] = {
    USER_CANCELLED: "操作者请求停止；运行已保存状态后终结。",
    HOST_SHUTDOWN: "宿主正常停机；旧请求没有自动重放。",
    WORKER_DISCONNECTED: "回合 worker 与宿主的通道断开（宿主崩溃或被杀）；运行未完成。",
    ORPHAN_RECOVERED: "宿主重启时发现此前未终结的回合；未自动重放。",
    HOST_EXCEPTION: "宿主在回合循环中捕获到异常。",
    WORKER_FAILED: "回合 worker 退出码非 0 或未报告 completed。",
}

#: Error text carried beside ``ORPHAN_RECOVERED``.  Named for the same reason
#: DeerFlow names ``STARTUP_ORPHAN_RECOVERY_ERROR``: the durable row should read
#: as a sentence, not as a status word a reader has to decode.
ORPHAN_RECOVERY_ERROR: Final = (
    "宿主在回合到达持久终态之前重启或崩溃；该回合未被重放，产物以磁盘为准。"
)

WORKER_DISCONNECTED_ERROR: Final = (
    "回合 worker 与宿主的通道断开（ConnectionResetError）；宿主或 worker 在被终结前退出。"
)


def describe(reason: str) -> str:
    """Human-readable text for a stop reason; unknown reasons stay verbatim."""
    return STOP_REASON_DESCRIPTIONS.get(str(reason), str(reason))


def terminal_for_worker_exit(
    *,
    returncode: int,
    completed_status: str,
    cancelled: bool,
    cancelled_at: bool,
    host_stopping: bool,
) -> tuple[str, str]:
    """``(status, stop_reason)`` for the ordinary end of a turn loop.

    Mirrors the historical status computation -- completion requires BOTH a zero
    worker exit code and the worker's own ``completed`` -- and adds the reason
    that computation always had available but never recorded.
    """
    if cancelled_at:
        if cancelled:
            return "cancelled", USER_CANCELLED
        return "interrupted", HOST_SHUTDOWN if host_stopping else WORKER_DISCONNECTED
    if returncode == 0 and completed_status == "completed":
        return "completed", ""
    return "failed", WORKER_FAILED