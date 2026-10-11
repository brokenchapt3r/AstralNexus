"""Explicit synchronous/asynchronous embedding contract."""
from __future__ import annotations
import asyncio
from typing import Any, Coroutine, Callable, TypeVar
T = TypeVar('T')


def run_sync(factory: Callable[[], Coroutine[Any, Any, T]], *, async_entry: str) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    # Check before constructing a coroutine, so callers get no unawaited warning.
    raise RuntimeError('An event loop is already running; await ' + async_entry)


async def run_async(runtime, goal=None, constraints=None, *, wave_hook=None):
    if runtime._interrupt_event is None:
        from threading import Event
        runtime._interrupt_event = Event()
    task = asyncio.create_task(asyncio.to_thread(runtime.run, goal=goal, constraints=constraints, wave_hook=wave_hook))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        runtime._interrupt_event.set()
        # Do not leave an invisible second manager behind after cancellation.
        await asyncio.shield(task)
        raise


async def execute_task_async(runtime, node_id):
    try:
        return await runtime._execute_task_async(node_id)
    except asyncio.CancelledError:
        if runtime._interrupt_event is None:
            from threading import Event
            runtime._interrupt_event = Event()
        runtime._interrupt_event.set()
        raise
