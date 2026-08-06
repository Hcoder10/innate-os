# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A dedicated asyncio loop on a daemon thread, running at most one root task."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future


class LoopThread:
    """The agent's runtime: spawn a root coroutine, cancel it synchronously.

    ``cancel()`` blocks until the task has fully unwound, so the caller can
    safely mutate state the task was using.
    """

    def __init__(self, name: str):
        self.loop = asyncio.new_event_loop()
        self._task: Future[None] | None = None
        self._done = threading.Event()
        self._done.set()  # no task has ever run: fully unwound
        self._thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def unwound(self) -> bool:
        """True when no root task is running or lingering mid-cancellation.

        False after a :meth:`cancel` timed out: the disowned task is still
        unwinding, and spawning over it would run two root tasks at once.
        """
        return self._done.is_set()

    def spawn(self, coro: Coroutine[object, object, None]) -> None:
        assert self._task is None, "root task already running"
        done = self._done = threading.Event()

        async def root():
            try:
                await coro
            finally:
                done.set()

        self._task = asyncio.run_coroutine_threadsafe(root(), self.loop)

    def cancel(self, timeout: float = 5.0) -> bool:
        """Cancel the root task and wait for it to unwind. False on timeout."""
        task, self._task = self._task, None
        if task is None:
            return True
        task.cancel()
        return self._done.wait(timeout)

    def post(self, fn: Callable[..., None], *args: object) -> None:
        """Run ``fn`` on the loop thread (thread-safe, no-op after shutdown)."""
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    def shutdown(self, timeout: float = 2.0) -> None:
        self.cancel()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self.loop.close()
