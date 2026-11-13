"""The bridge that runs the asynchronous store from the synchronous tool threads."""

import asyncio
import threading

import pytest

from plugins.accomy_travel import orderbridge


def test_a_coroutine_runs_on_the_bridge_loop_and_its_result_comes_back():
    async def answer():
        return threading.current_thread().name

    assert orderbridge.run(answer) == "accomy-orders"


def test_a_call_that_outlives_its_timeout_is_cancelled_and_raises():
    cancelled = threading.Event()

    async def slow():
        never_set = asyncio.Event()
        try:
            await never_set.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        orderbridge.run(slow, timeout=0.05)
    assert cancelled.wait(5)


def test_a_forked_child_starts_its_own_loop(monkeypatch):
    async def answer():
        return 42

    before = orderbridge._event_loop()
    monkeypatch.setattr(orderbridge.os, "getpid", lambda: orderbridge._loop_pid + 1)

    assert orderbridge.run(answer) == 42
    assert orderbridge._event_loop() is not before
