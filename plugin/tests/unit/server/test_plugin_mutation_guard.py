from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Any

import pytest

from plugin.server.application.plugins.mutation_guard import plugin_mutation_guard


class _CountingExecutor(ThreadPoolExecutor):
    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self._count_lock = threading.Lock()
        self.submission_count = 0

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        with self._count_lock:
            self.submission_count += 1
        return super().submit(fn, *args, **kwargs)

    def reset_count(self) -> None:
        with self._count_lock:
            self.submission_count = 0


@pytest.mark.asyncio
async def test_waiting_for_mutation_guard_does_not_consume_default_executor() -> None:
    loop = asyncio.get_running_loop()
    previous_executor = getattr(loop, "_default_executor", None)
    executor = _CountingExecutor()
    loop.set_default_executor(executor)
    waiter_attempted = asyncio.Event()
    waiter_entered = asyncio.Event()
    waiter_task: asyncio.Task[None] | None = None

    async def waiter() -> None:
        waiter_attempted.set()
        async with plugin_mutation_guard():
            waiter_entered.set()

    try:
        async with plugin_mutation_guard():
            executor.reset_count()
            waiter_task = asyncio.create_task(waiter())
            await waiter_attempted.wait()
            scheduling_barrier = asyncio.Event()
            loop.call_soon(scheduling_barrier.set)
            await scheduling_barrier.wait()
            assert executor.submission_count == 0
            await asyncio.to_thread(lambda: None)
            assert executor.submission_count == 1
            assert not waiter_entered.is_set()
        await waiter_task
        assert waiter_entered.is_set()
    finally:
        if waiter_task is not None:
            await asyncio.gather(waiter_task, return_exceptions=True)
        loop._default_executor = previous_executor  # type: ignore[attr-defined]
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_canceled_mutation_waiter_does_not_keep_or_steal_lock() -> None:
    waiter_attempted = asyncio.Event()

    async def waiter() -> None:
        waiter_attempted.set()
        async with plugin_mutation_guard():
            raise AssertionError("canceled waiter must not enter the guard")

    async with plugin_mutation_guard():
        waiter_task = asyncio.create_task(waiter())
        await waiter_attempted.wait()
        waiter_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    async with plugin_mutation_guard():
        pass
