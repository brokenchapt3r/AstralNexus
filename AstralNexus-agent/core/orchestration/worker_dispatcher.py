"""Graph-free worker calls whose cancellation never joins a stuck sync thread."""
from __future__ import annotations
import asyncio
from concurrent.futures import Future
from threading import Thread
from contextvars import copy_context


class WorkerDispatcher:
    async def call(self, function, *, stage=None):
        future: Future = Future()
        def worker():
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = function()
                if stage is not None:
                    stage(result)
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)
        Thread(target=copy_context().run, args=(worker,), name='astralnexus-worker-call', daemon=True).start()
        return await asyncio.wrap_future(future)
