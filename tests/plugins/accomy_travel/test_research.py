"""Behaviour of research_web: scope refusals, Brave request shape, sanitization and per-caller limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.accomy_travel import _common, tools_research

FIXTURES = Path(__file__).parent / "fixtures"
BRAVE_URL = "https://api.search.brave.com/res/v1/llm/context"
KEY = "test-brave-key"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBrave:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = (
            body
            if body is not None
            else json.loads((FIXTURES / "brave_llm_context.json").read_text("utf-8"))
        )
        self.calls: list[dict] = []

    def __call__(self, method: str, url: str, **kwargs) -> tuple[int, object]:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.status, self.body


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr(
        "plugins.accomy_travel._common._limiter", _common.RateLimiter(clock=fake_clock)
    )
    monkeypatch.setattr(
        tools_research, "_limiter", _common.RateLimiter(clock=fake_clock)
    )
    monkeypatch.setattr("plugins.accomy_travel._common.caller_id", lambda: "guest-1")
    monkeypatch.setattr("plugins.accomy_travel._common.secret", lambda name: KEY)
    return fake_clock


@pytest.fixture
def brave(monkeypatch, clock) -> FakeBrave:
    fake = FakeBrave()
    monkeypatch.setattr("plugins.accomy_travel._common.request_json", fake)
    return fake


def _research(args: dict) -> dict:
    handler = next(
        spec.handler for spec in tools_research.TOOLS if spec.name == "research_web"
    )
    return json.loads(handler(args))


def test_research_returns_sanitized_sources_and_sends_the_token(brave):
    out = _research(
        {"query": "Japan visa exemption for Taiwan passport holders", "freshness": "py"}
    )

    call = brave.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == BRAVE_URL
    assert call["headers"]["X-Subscription-Token"] == KEY
    assert call["params"]["freshness"] == "py"
    assert call["params"]["q"] == "Japan visa exemption for Taiwan passport holders"
    assert out["status"] == "ok"
    assert out["note"] == _common.UNTRUSTED_NOTE
    assert "Cite the URLs" in out["hint"]
    first = out["results"][0]
    assert first["url"] == "https://www.mofa.go.jp/j_info/visit/visa/short/novisa.html"
    assert first["hostname"] == "www.mofa.go.jp"
    assert first["date"] == "2025-09-01"
    assert first["snippets"][0].startswith("| Asia | Europe |")


def test_research_drops_unsafe_or_empty_sources_and_caps_snippets(brave):
    brave.body = {
        "grounding": {
            "generic": [
                {"url": "javascript:alert(1)", "title": "Bad", "snippets": ["x"]},
                {"url": "https://example.org/empty", "title": "Empty", "snippets": []},
                {
                    "url": "https://example.org/long",
                    "title": "<b>Long</b> page",
                    "snippets": ["<p>Hi</p>"]
                    + ["word " * 200]
                    + ["s3", "s4", "s5", "s6"],
                },
            ]
        },
        "sources": {},
    }

    out = _research({"query": "entry rules for Thailand"})

    assert [r["url"] for r in out["results"]] == ["https://example.org/long"]
    result = out["results"][0]
    assert result["title"] == "Long page"
    assert result["snippets"][0] == "Hi"
    assert len(result["snippets"]) == 5
    assert all(len(snippet) <= 500 for snippet in result["snippets"])
    assert "date" not in result


def test_research_reports_no_results_when_brave_returns_no_sources(brave):
    brave.body = {"grounding": {"generic": [], "map": []}, "sources": {}}

    out = _research({"query": "xyzzy plugh"})

    assert out == {
        "status": "no_results",
        "results": [],
        "hint": "No sources matched. Try different keywords.",
    }


@pytest.mark.parametrize(
    ("query", "category"),
    [
        ("write a python scraper for flight prices", "code"),
        ("how to hack into my neighbour's wifi", "security"),
        ("port scan the hotel network for open ports", "security"),
        ("home address of my ex-partner", "privacy"),
        ("integral of x squared from 0 to 3", "math"),
    ],
)
def test_out_of_scope_queries_are_refused_before_any_brave_call(brave, query, category):
    out = _research({"query": query})

    assert out["status"] == "refused"
    assert out["reason"] == "out_of_scope"
    assert out["category"] == category
    assert brave.calls == []


@pytest.mark.parametrize(
    "query",
    [
        "Narita airport security scan rules",
        "travel in Java island in Indonesia this winter",
        "create an itinerary for a week in Taiwan",
    ],
)
def test_travel_phrasings_that_look_close_to_refused_topics_still_reach_brave(
    brave, query
):
    out = _research({"query": query})

    assert out["status"] == "ok"
    assert len(brave.calls) == 1


def test_empty_and_overlong_queries_are_refused_without_a_call(brave):
    assert _research({"query": "   "}) == {
        "status": "refused",
        "reason": "empty_query",
        "hint": "Pass a focused search query.",
    }
    too_long = _research({"query": "visa " * 80})
    assert too_long["reason"] == "query_too_long"
    assert brave.calls == []


def test_unknown_freshness_is_an_argument_error(brave):
    out = _research({"query": "festival dates in Kyoto", "freshness": "yesterday"})

    assert out["code"] == "invalid_arguments"
    assert brave.calls == []


def test_brave_rate_limit_is_relayed_as_a_status(brave):
    brave.status = 429
    brave.body = {}

    assert _research({"query": "current train strikes in France"}) == {
        "status": "rate_limited"
    }


def test_other_brave_failures_surface_as_unavailable(brave):
    brave.status = 500
    brave.body = {"error": "internal"}

    out = _research({"query": "current train strikes in France"})

    assert out["code"] == "unavailable"


def test_per_minute_limit_blocks_the_eleventh_call_until_the_window_passes(
    brave, clock
):
    for _ in range(10):
        assert _research({"query": "festival dates in Kyoto"})["status"] == "ok"

    assert _research({"query": "festival dates in Kyoto"}) == {"status": "rate_limited"}
    assert len(brave.calls) == 10

    clock.now += 61
    assert _research({"query": "festival dates in Kyoto"})["status"] == "ok"


def test_per_day_limit_stops_the_hundred_and_first_call(brave, clock):
    for _ in range(10):
        for _ in range(10):
            assert _research({"query": "festival dates in Kyoto"})["status"] == "ok"
        clock.now += 61

    assert _research({"query": "festival dates in Kyoto"}) == {"status": "rate_limited"}
    assert len(brave.calls) == 100


def test_limits_are_kept_per_caller(brave, monkeypatch, clock):
    for _ in range(10):
        _research({"query": "festival dates in Kyoto"})
    assert _research({"query": "festival dates in Kyoto"}) == {"status": "rate_limited"}

    monkeypatch.setattr("plugins.accomy_travel._common.caller_id", lambda: "guest-2")

    assert _research({"query": "festival dates in Kyoto"})["status"] == "ok"
