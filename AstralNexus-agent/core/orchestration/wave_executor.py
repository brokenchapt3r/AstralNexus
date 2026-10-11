"""Single-owner graph scheduling with parallel, graph-free worker calls."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from typing import Awaitable, Callable


@dataclass(frozen=True)
class WorkerResult:
    node_id: str
    succeeded: bool | None
    error: Exception | None = None


class WaveTimeoutError(TimeoutError):
    def __init__(self, *, pending: list[str], waiting: list[str]):
        self.pending = pending
        self.waiting = waiting
        super().__init__("Worker wave deadline exceeded; pending=" + ",".join(pending) + "; staged=" + ",".join(waiting))


class CommitBarrier:
    """Do not commit a worker response while a peer still consumes its inputs."""
    def __init__(self, node_ids: list[str]):
        self.pending = set(node_ids)
        self.waiting: set[str] = set()
        self.ready = asyncio.Event()

    def arrive(self, node_id: str) -> None:
        self.pending.discard(node_id)
        if not self.pending:
            self.ready.set()

    async def wait(self, node_id: str) -> None:
        self.waiting.add(node_id)
        self.arrive(node_id)
        try:
            await self.ready.wait()
        finally:
            self.waiting.discard(node_id)


class WaveExecutor:
    """Own the event loop; thread workers receive only prepared agent contexts.

    Context preparation, result staging and all graph edits run on the caller's
    thread. The barrier separates parallel model/tool work from serial commits.
    """
    def run(
        self, node_ids: list[str], *, parallel: int,
        execute: Callable[[str], Awaitable[bool | None]],
        set_barrier: Callable[[CommitBarrier | None], None],
        on_result: Callable[[WorkerResult], None],
        cancelled: Callable[[], bool],
    ) -> None:
        from .async_api import run_sync
        return run_sync(lambda: self.run_async(node_ids, parallel=parallel, execute=execute,
                        set_barrier=set_barrier, on_result=on_result, cancelled=cancelled), async_entry="WaveExecutor.run_async(...)")

    async def run_async(
        self, node_ids: list[str], *, parallel: int,
        execute: Callable[[str], Awaitable[bool | None]],
        set_barrier: Callable[[CommitBarrier | None], None],
        on_result: Callable[[WorkerResult], None],
        cancelled: Callable[[], bool],
    ) -> None:
        timeout = max(.01, float(os.environ.get("ASTRALNEXUS_WAVE_TIMEOUT", "1800")))
        deadline = asyncio.get_running_loop().time() + timeout
        for offset in range(0, len(node_ids), max(1, parallel)):
            if cancelled():
                break
            batch = node_ids[offset:offset + max(1, parallel)]
            barrier = CommitBarrier(batch)
            set_barrier(barrier)
            async def job(node_id: str) -> WorkerResult:
                try:
                    return WorkerResult(node_id, await execute(node_id))
                except Exception as exc:
                    return WorkerResult(node_id, False, exc)
                finally:
                    # A setup failure has no model result to reach wait().
                    barrier.arrive(node_id)
            tasks = [asyncio.create_task(job(node_id)) for node_id in batch]
            try:
                done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
                if pending:
                    failure = WaveTimeoutError(pending=sorted(barrier.pending), waiting=sorted(barrier.waiting))
                    # Cancel all actor continuations. The completed model data
                    # is already staged, so no partial graph commit is needed.
                    for task in pending:
                        task.cancel()
                    raise failure
                for task in tasks:
                    on_result(task.result())
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
                set_barrier(None)


def invoke_worker(agent, context):
    """Install thread-local service ownership on the actual tool worker thread."""
    from AstralNexus.tools.builtin_tools import set_tool_node_context
    set_tool_node_context(context.node_id)
    try:
        return agent.execute(context)
    finally:
        set_tool_node_context("")
