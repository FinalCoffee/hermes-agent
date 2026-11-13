"""The bridge from the synchronous tool handlers to the asynchronous order store.

Tool handlers run on the gateway's thread pool and return a string, while the store is asyncpg. One daemon thread runs
one asyncio loop for the whole process, and each call opens its own connection on it: no pool or connection is shared
between threads or profiles. A forked child starts its own loop, because a thread does not survive fork.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
CALL_TIMEOUT_SECONDS = 20.0

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_pid: int | None = None


def _event_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_pid
    with _lock:
        if _loop is None or _loop_pid != os.getpid() or not _loop.is_running():
            loop = asyncio.new_event_loop()
            # The loop runs only coroutines that receive their DSN as an argument and reads no profile state.
            threading.Thread(  # health: allow HX012 -- no profile state is read on this thread (see above)
                target=loop.run_forever, name="accomy-orders", daemon=True).start()
            _loop, _loop_pid = loop, os.getpid()
        return _loop


def run(make: Callable[[], Awaitable[T]], *, timeout: float = CALL_TIMEOUT_SECONDS) -> T:
    """Run ``make()`` on the loop and wait for its result. A call still running after *timeout* is cancelled, which
    closes its connection, and raises TimeoutError."""
    future = asyncio.run_coroutine_threadsafe(make(), _event_loop())
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise
