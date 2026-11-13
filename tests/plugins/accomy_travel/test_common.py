"""Shared plumbing of the Accomy travel tools: the per-caller rate limiter and the handler wrapper."""

import json

import pytest

from plugins.accomy_travel import _common


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_refused_check_charges_none_of_the_windows_in_the_same_call():
    clock = _Clock()
    limiter = _common.RateLimiter(clock=clock)
    assert limiter.allow("global", 1, 60.0)  # the global window is already full

    refused = limiter.admit((("ana:minute", 5, 60.0), ("ana:day", 5, 86400.0), ("global", 1, 60.0)))

    assert refused == 2
    assert limiter.admit((("ana:minute", 1, 60.0), ("ana:day", 1, 86400.0))) is None  # nothing was spent
    assert limiter.admit((("ana:minute", 1, 60.0),)) == 0


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


class _Wire:
    """Stands in for httpx.request so a test counts the supplier calls that really leave the process."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, method, url, **_kwargs):
        self.urls.append(url)
        return type("Response", (), {"status_code": 200, "json": staticmethod(lambda: {"data": []})})()


def _fresh_supplier_budget(monkeypatch, clock):
    # raising=False: on a tree without the budget the test must fail on the missing budget, not on setup.
    monkeypatch.setattr(_common, "_supplier_budget", _common.RateLimiter(clock=clock), raising=False)


def test_seat_unique_host_budget_refuses_the_101st_request_without_a_call(monkeypatch):
    clock = _Clock()
    _fresh_supplier_budget(monkeypatch, clock)
    wire = _Wire()
    monkeypatch.setattr(_common.httpx, "request", wire)
    url = "https://api.sandbox.dev.seatunique.tech/trade_api/v1/categories"

    for _ in range(100):
        _common.request_json("GET", url)
    with pytest.raises(_common.ApiError) as refused:
        _common.request_json("GET", url)

    assert refused.value.code == "rate_limited"
    assert len(wire.urls) == 100

    clock.now += 60.0
    _common.request_json("GET", url)
    assert len(wire.urls) == 101


def test_supplier_daily_budget_is_per_host_and_shared_by_every_caller(monkeypatch):
    clock = _Clock()
    _fresh_supplier_budget(monkeypatch, clock)
    wire = _Wire()
    monkeypatch.setattr(_common.httpx, "request", wire)
    url = "https://maps.googleapis.com/v1/thing"

    # Calls pace at 50 a minute, under every per-minute budget, so only the host's day budget can refuse. The cap
    # of 10,000 is far above any day budget, so the loop ends on the refusal and not on the bound.
    admitted = 0
    with pytest.raises(_common.ApiError) as refused:
        while admitted < 10_000:
            _common.request_json("GET", url)
            admitted += 1
            if admitted % 50 == 0:
                clock.now += 60.0
    assert refused.value.code == "rate_limited"
    assert len(wire.urls) == admitted

    # The day budget belongs to the host, so another host is untouched.
    _common.request_json("GET", "https://weather.googleapis.com/v1/thing")
    assert len(wire.urls) == admitted + 1


def test_a_caller_past_the_daily_tool_cap_is_refused_until_the_next_day(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter(clock=clock))
    monkeypatch.setattr(_common, "CALLS_PER_MINUTE", 1000)
    monkeypatch.setattr(_common, "CALLS_PER_DAY", 3, raising=False)
    callers = {"who": "ana"}
    monkeypatch.setattr(_common, "caller_id", lambda: callers["who"])

    @_common.tool_handler
    def lookup(args):
        return _common.ok(done=True)

    assert [json.loads(lookup({})).get("code") for _ in range(3)] == [None, None, None]
    assert json.loads(lookup({}))["code"] == "rate_limited"

    callers["who"] = "ben"  # one caller's day never spends another's
    assert json.loads(lookup({})).get("code") is None

    callers["who"] = "ana"
    clock.now += 86400.0
    assert json.loads(lookup({})).get("code") is None
