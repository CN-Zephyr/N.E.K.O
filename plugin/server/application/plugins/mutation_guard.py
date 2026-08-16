from __future__ import annotations

import asyncio
from collections import deque
import threading
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any


class _MutationWaiter:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        self.state = "waiting"


class _AsyncMutationLock:
    """Process-wide async mutex whose waiters never occupy an executor thread."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._held = False
        self._waiters: deque[_MutationWaiter] = deque()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if not self._held:
                self._held = True
                return
            waiter = _MutationWaiter(loop)
            self._waiters.append(waiter)

        try:
            await waiter.future
        except asyncio.CancelledError:
            wake: _MutationWaiter | None = None
            with self._state_lock:
                if waiter.state == "waiting":
                    waiter.state = "cancelled"
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                elif waiter.state == "granted":
                    waiter.state = "cancelled"
                    wake = self._handoff_locked()
            self._schedule_wake(wake)
            raise

        with self._state_lock:
            if waiter.state != "granted":  # pragma: no cover - defensive invariant
                raise RuntimeError("plugin mutation lock waiter lost its grant")
            waiter.state = "acquired"

    def release(self) -> None:
        with self._state_lock:
            if not self._held:
                raise RuntimeError("plugin mutation lock is not held")
            wake = self._handoff_locked()
        self._schedule_wake(wake)

    def _handoff_locked(self) -> _MutationWaiter | None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if (
                waiter.state != "waiting"
                or waiter.future.cancelled()
                or waiter.loop.is_closed()
            ):
                waiter.state = "cancelled"
                continue
            waiter.state = "granted"
            return waiter
        self._held = False
        return None

    def _schedule_wake(self, waiter: _MutationWaiter | None) -> None:
        if waiter is None:
            return
        try:
            waiter.loop.call_soon_threadsafe(self._finish_wake, waiter)
        except RuntimeError:
            self._abandon_grant(waiter)

    def _finish_wake(self, waiter: _MutationWaiter) -> None:
        wake: _MutationWaiter | None = None
        with self._state_lock:
            if waiter.state != "granted":
                return
            if waiter.future.cancelled() or waiter.loop.is_closed():
                waiter.state = "cancelled"
                wake = self._handoff_locked()
            else:
                waiter.future.set_result(None)
        self._schedule_wake(wake)

    def _abandon_grant(self, waiter: _MutationWaiter) -> None:
        with self._state_lock:
            if waiter.state != "granted":
                return
            waiter.state = "cancelled"
            wake = self._handoff_locked()
        self._schedule_wake(wake)


_MUTATION_LOCK = _AsyncMutationLock()
_MUTATION_DEPTH: ContextVar[int] = ContextVar(
    "plugin_mutation_depth",
    default=0,
)
_MUTATION_OWNER: ContextVar[asyncio.Task[Any] | None] = ContextVar(
    "plugin_mutation_owner",
    default=None,
)


class _PluginMutationGuard:
    def __init__(self) -> None:
        self._token: Token[int] | None = None
        self._owner_token: Token[asyncio.Task[Any] | None] | None = None
        self._acquired = False

    async def __aenter__(self) -> None:
        current_task = asyncio.current_task()
        if current_task is None:  # pragma: no cover - async context always has a task
            raise RuntimeError("plugin mutation guard requires an asyncio task")
        depth = _MUTATION_DEPTH.get()
        if depth and _MUTATION_OWNER.get() is current_task:
            self._token = _MUTATION_DEPTH.set(depth + 1)
            return

        await _MUTATION_LOCK.acquire()
        self._acquired = True
        self._owner_token = _MUTATION_OWNER.set(current_task)
        self._token = _MUTATION_DEPTH.set(1)

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        assert self._token is not None
        _MUTATION_DEPTH.reset(self._token)
        if self._acquired:
            assert self._owner_token is not None
            _MUTATION_OWNER.reset(self._owner_token)
            _MUTATION_LOCK.release()
        return False


def plugin_mutation_guard() -> _PluginMutationGuard:
    """Serialize plugin filesystem and metadata mutations in this process."""

    return _PluginMutationGuard()
