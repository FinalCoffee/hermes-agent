"""Shared plumbing of the Accomy travel tools: the per-caller rate limiter and the handler wrapper."""

import json

from plugins.accomy_travel import _common


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_caller_is_refused_at_the_limit_and_admitted_again_after_the_window():
    clock = _Clock()
    limiter = _common.RateLimiter(clock=clock)

    assert all(limiter.allow("ana", 3, 60.0) for _ in range(3))
    assert not limiter.allow("ana", 3, 60.0)
    assert limiter.allow("ben", 3, 60.0)  # one caller's burst never spends another's allowance

    clock.now += 60.0
    assert limiter.allow("ana", 3, 60.0)


def test_a_daily_window_survives_minute_traffic_from_other_callers():
    """One limiter serves a per-minute and a per-day window: forgetting idle minute callers must
    not hand a caller back their daily allowance."""
    clock = _Clock()
    limiter = _common.RateLimiter(clock=clock)

    assert limiter.allow("ana:day", 1, 86400.0)
    for minute in range(1, 6):
        clock.now += 61.0
        assert limiter.allow(f"visitor-{minute}:minute", 10, 60.0)

    assert not limiter.allow("ana:day", 1, 86400.0)


def test_callers_whose_window_has_passed_no_longer_take_memory():
    clock = _Clock()
    limiter = _common.RateLimiter(clock=clock)
    for index in range(500):
        limiter.allow(f"visitor-{index}", 30, 60.0)

    clock.now += 120.0
    limiter.allow("latest", 30, 60.0)

    assert len(limiter._hits) == 1


def test_handler_turns_supplier_and_argument_failures_into_error_results(monkeypatch):
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())

    @_common.tool_handler
    def supplier_down(args):
        raise _common.ApiError("unavailable", "The service is down.", status=503)

    @_common.tool_handler
    def bad_argument(args):
        raise ValueError("check_in must be a date.")

    assert json.loads(supplier_down({})) == {"error": "The service is down.", "code": "unavailable"}
    assert json.loads(bad_argument({})) == {"error": "check_in must be a date.", "code": "invalid_arguments"}


def test_handler_refuses_a_caller_who_exceeds_the_per_minute_allowance(monkeypatch):
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    monkeypatch.setattr(_common, "CALLS_PER_MINUTE", 2)
    calls = []

    @_common.tool_handler
    def lookup(args):
        calls.append(args)
        return _common.ok(done=True)

    results = [json.loads(lookup({})) for _ in range(3)]

    assert [r.get("code") for r in results] == [None, None, "rate_limited"]
    assert len(calls) == 2
