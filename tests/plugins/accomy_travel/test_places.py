"""Behaviour of the Google place and weather tools, exercised through their registered handlers.

Every HTTP exchange goes through ``_common.request_json``, so one fake records what the tools send and
answers with a canned Google response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.accomy_travel import _common, tools_places

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAIL_URL = "https://places.googleapis.com/v1/places/ChIJCewJkL2LGGAR3Qmk0vCTGkg"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
CURRENT_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"
FORECAST_URL = "https://weather.googleapis.com/v1/forecast/days:lookup"
KEY = "test-google-key"

GEOCODE_OK = {
    "status": "OK",
    "results": [
        {
            "formatted_address": "Kyoto Station, Kyoto, Japan",
            "place_id": "ChIJkyotostation0001",
            "geometry": {"location": {"lat": 34.9858, "lng": 135.7588}},
        }
    ],
}
GEOCODE_NONE = {"status": "ZERO_RESULTS", "results": []}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeGoogle:
    """Routes each request to the first canned (status, body) whose URL prefix matches."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, int, object]] = []
        self.calls: list[dict] = []

    def route(self, prefix: str, status: int, body: object) -> None:
        self.routes.append((prefix, status, body))

    def __call__(self, method: str, url: str, **kwargs) -> tuple[int, object]:
        self.calls.append({"method": method, "url": url, **kwargs})
        for prefix, status, body in self.routes:
            if url.startswith(prefix):
                return status, body
        raise AssertionError(f"unexpected request to {url}")


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr("plugins.accomy_travel._common.request_json", fake)
    monkeypatch.setattr("plugins.accomy_travel._common.secret", lambda name: KEY)
    monkeypatch.setattr("plugins.accomy_travel._common._limiter", _common.RateLimiter())
    return fake


def _handler(name: str):
    return next(spec.handler for spec in tools_places.TOOLS if spec.name == name)


def test_find_places_sends_key_field_mask_and_capped_page_size(google):
    google.route(SEARCH_URL, 200, _fixture("g_places_text.json"))

    _handler("find_places")({"query": "ramen Kyoto", "max_results": 8})

    call = google.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["X-Goog-Api-Key"] == KEY
    mask = call["headers"]["X-Goog-FieldMask"].split(",")
    assert "places.displayName" in mask
    assert "places.reviews" not in mask
    assert call["json_body"] == {
        "textQuery": "ramen Kyoto",
        "languageCode": "en",
        "pageSize": 8,
    }


def test_find_places_returns_compact_places_with_directions_link(google):
    google.route(SEARCH_URL, 200, _fixture("g_places_text.json"))

    out = json.loads(_handler("find_places")({"query": "Kyoto landmarks"}))

    assert out["note"] == _common.UNTRUSTED_NOTE
    first, second = out["places"][0], out["places"][1]
    assert first["place_id"] == "ChIJT8uMzZwIAWARnGzsARCjnrY"
    assert first["name"] == "錦市場"
    assert first["rating"] == 4.3 and first["review_count"] == 55553
    assert (first["lat"], first["lng"]) == (35.005026, 135.764723)
    assert first["summary"].startswith("擁有 400 年")
    assert first["directions_url"] == (
        "https://www.google.com/maps/dir/?api=1&destination=35.005026,135.764723"
        "&destination_place_id=ChIJT8uMzZwIAWARnGzsARCjnrY"
    )
    assert "open_now" not in first
    assert second["type"] == "城堡"
    assert second["open_now"] is False


def test_near_geocodes_the_anchor_and_biases_the_search(google):
    google.route(GEOCODE_URL, 200, GEOCODE_OK)
    google.route(SEARCH_URL, 200, _fixture("g_places_text.json"))

    out = json.loads(
        _handler("find_places")(
            {"query": "ramen", "near": "京都車站", "language": "zh-TW"}
        )
    )

    assert google.calls[0]["params"]["address"] == "京都車站"
    body = google.calls[1]["json_body"]
    assert body["languageCode"] == "zh-TW"
    assert body["locationBias"] == {
        "circle": {
            "center": {"latitude": 34.9858, "longitude": 135.7588},
            "radius": 10000.0,
        }
    }
    assert out["searched_near"] == "Kyoto Station, Kyoto, Japan"


def test_open_now_and_min_rating_reach_the_search_body(google):
    google.route(SEARCH_URL, 200, _fixture("g_places_text.json"))

    _handler("find_places")({"query": "cafe", "open_now": True, "min_rating": 4})

    body = google.calls[0]["json_body"]
    assert body["openNow"] is True
    assert body["minRating"] == 4.0


def test_find_places_reports_no_results_with_a_hint(google):
    google.route(SEARCH_URL, 200, {})

    out = json.loads(_handler("find_places")({"query": "zzzz nowhere"}))

    assert out["status"] == "no_results"
    assert out["places"] == []
    assert "hint" in out


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": "x", "max_results": 9},
        {"query": "x", "open_now": "yes"},
        {"query": "x", "language": "english please"},
        {"query": "x", "min_rating": 7},
    ],
)
def test_find_places_rejects_bad_arguments_before_any_request(google, args):
    out = json.loads(_handler("find_places")(args))

    assert out["code"] == "invalid_arguments"
    assert google.calls == []


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (
            403,
            {
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": "API key not valid for project 123",
                }
            },
            "unavailable",
        ),
        (429, {}, "rate_limited"),
        (400, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "rate_limited"),
    ],
)
def test_find_places_maps_supplier_errors_without_leaking_their_text(
    google, status, body, code
):
    google.route(SEARCH_URL, status, body)

    out = json.loads(_handler("find_places")({"query": "museum"}))

    assert out["code"] == code
    assert "project 123" not in out["error"]


def test_place_details_returns_hours_phone_and_three_cleaned_reviews(google):
    google.route(DETAIL_URL, 200, _fixture("g_place_details.json"))

    out = json.loads(
        _handler("get_place_details")(
            {"place_id": "ChIJCewJkL2LGGAR3Qmk0vCTGkg", "language": "zh-TW"}
        )
    )

    call = google.calls[0]
    assert call["method"] == "GET"
    assert call["params"] == {"languageCode": "zh-TW"}
    assert "places." not in call["headers"]["X-Goog-FieldMask"]
    assert out["note"] == _common.UNTRUSTED_NOTE
    assert out["phone"] == "+81 3-3433-5111"
    assert len(out["hours"]) == 2
    reviews = out["reviews"]
    assert len(reviews) == 3
    assert "<b>" not in reviews[0]["text"] and "Book early" in reviews[0]["text"]
    assert len(reviews[1]["text"]) <= 240 and reviews[1]["text"].endswith("…")
    assert "Fourth review" not in json.dumps(out, ensure_ascii=False)


def test_place_details_rejects_an_id_that_could_alter_the_url(google):
    out = json.loads(
        _handler("get_place_details")({"place_id": "../../v1/places:searchText"})
    )

    assert out["code"] == "invalid_arguments"
    assert google.calls == []


def test_place_details_reports_unknown_place_as_not_found(google):
    google.route(DETAIL_URL, 404, {"error": {"status": "NOT_FOUND"}})

    out = json.loads(
        _handler("get_place_details")({"place_id": "ChIJCewJkL2LGGAR3Qmk0vCTGkg"})
    )

    assert out["code"] == "not_found"


def test_weather_combines_current_conditions_and_local_daily_forecast(google):
    google.route(GEOCODE_URL, 200, GEOCODE_OK)
    google.route(CURRENT_URL, 200, _fixture("g_weather_current.json"))
    google.route(FORECAST_URL, 200, _fixture("g_weather_days.json"))

    out = json.loads(_handler("get_weather")({"location": "Kyoto", "days": 2}))

    assert out["location"] == {
        "resolved_address": "Kyoto Station, Kyoto, Japan",
        "timezone": "Asia/Tokyo",
    }
    assert out["current"]["condition"] == "晴"
    assert out["current"]["temp_c"] == 11.9
    assert out["current"]["humidity_pct"] == 75
    daily = out["daily"]
    assert [day["date"] for day in daily] == ["2026-11-12", "2026-11-13"]
    assert daily[0]["max_c"] == 11 and daily[1]["min_c"] == 8
    assert daily[0]["rain_chance_pct"] == 20
    assert daily[0]["wind"] == {"speed_kmh": 6, "direction": "NORTHWEST"}
    # Sunrise is reported in the place's own zone (UTC+9), not in UTC.
    assert daily[0]["sunrise"] == "06:13"
    assert daily[0]["sunset"] == "16:36"
    forecast_call = google.calls[2]
    assert forecast_call["params"]["days"] == 2
    assert forecast_call["params"]["key"] == KEY


@pytest.mark.parametrize("days", [0, 11, "many"])
def test_weather_rejects_days_outside_one_to_ten(google, days):
    out = json.loads(_handler("get_weather")({"location": "Kyoto", "days": days}))

    assert out["code"] == "invalid_arguments"
    assert google.calls == []


def test_weather_reports_an_unknown_location(google):
    google.route(GEOCODE_URL, 200, GEOCODE_NONE)

    out = json.loads(_handler("get_weather")({"location": "Atlantis"}))

    assert out["code"] == "not_found"
    assert len(google.calls) == 1


def test_every_place_and_weather_tool_is_gated_on_the_google_key():
    specs = {spec.name: spec for spec in tools_places.TOOLS}

    assert set(specs) == {"find_places", "get_place_details", "get_weather"}
    assert all(
        spec.requires_env == ("GOOGLE_MAPS_SERVER_API_KEY",) for spec in specs.values()
    )
