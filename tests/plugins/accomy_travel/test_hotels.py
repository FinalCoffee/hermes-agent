"""Hotel tools through their handlers: destination resolution, date and party checks, result shaping and errors.

Fixtures are trimmed real TMC answers (tests/plugins/accomy_travel/fixtures/tmc_hotel_*.json). The supplier is a
fake transport, so nothing leaves the process.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from plugins.accomy_travel import _common, fx, geo, quotes, tmc_client, tools_hotels

FIXTURES = Path(__file__).parent / "fixtures"
APP_KEY = "app-key-for-tests"
SECRET = "secret-for-tests"
TOKEN_PATH = "/openapi/v1/tmc/auth/token"
SEARCH_PATH = "/openapi/v1/tmc/hotel/search"
LOCATIONS_PATH = "/openapi/v1/tmc/hotel/locations"
PRODUCTS_PATH = "/openapi/v1/tmc/hotel/products/search"
DETAIL_PATH = "/openapi/v1/tmc/hotel/10235214"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTmc:
    """Records every request as ``(method, path)`` with the decoded JSON body of each POST."""

    def __init__(self, routes: dict) -> None:
        self.routes = {
            ("POST", TOKEN_PATH): (200, {"code": "0", "data": {"token": "session-1"}}),
            **routes,
        }
        self.requests: list[tuple[str, str]] = []
        self.bodies: dict[str, list] = {}

    def __call__(
        self,
        method,
        url,
        *,
        headers=None,
        params=None,
        json_body=None,
        content=None,
        timeout=None,
    ):
        path = url.split("?", 1)[0].removeprefix(tmc_client.BASE_URL)
        self.requests.append((method, path))
        if content:
            self.bodies.setdefault(path, []).append(json.loads(content))
        response = self.routes[(method, path)]
        if isinstance(response, list):  # one answer per request, in order
            return response.pop(0)
        return response


@pytest.fixture(autouse=True)
def _hotel_state(monkeypatch):
    tmc_client._tokens.clear()
    tmc_client._airport_cache.clear()
    tools_hotels._hotels.clear()
    quotes._quotes.clear()
    monkeypatch.setattr(
        _common,
        "secret",
        lambda name: {"TMC_APP_KEY": APP_KEY, "TMC_APP_SECRET": SECRET}.get(name, ""),
    )
    monkeypatch.setattr(_common, "caller_id", lambda: "user-a")
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    yield
    tmc_client._tokens.clear()


def _install(monkeypatch, routes: dict) -> FakeTmc:
    # The hotel's name comes from its detail page when search has not shown it, so every fake answers that read.
    detail = ("GET", DETAIL_PATH)
    fake = FakeTmc({detail: (200, _fixture("tmc_hotel_detail.json")), **routes})
    monkeypatch.setattr(_common, "request_json", fake)
    return fake


def _search_route(payload: dict | None = None):
    """The search answer, always the last page: the fixture's totalPage (88) is replaced by 1 because this fake
    answers every request the same way, so a longer list would make the tool re-read the same rows."""
    data = {**copy.deepcopy(payload if payload is not None else _fixture("tmc_hotel_search.json")["data"]), "totalPage": 1}
    return ("POST", SEARCH_PATH), (200, {"code": "0", "data": data})


def _unpriced(hotel: dict) -> dict:
    return {**hotel, "lowestPrice": 0}


def _locations_route():
    return ("GET", LOCATIONS_PATH), (200, _fixture("tmc_hotel_locations.json"))


def _error_code(result: str) -> str:
    body = json.loads(result)
    assert "error" in body, body
    return body["code"]


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _search_args(**overrides) -> dict:
    args = {"destination": "Tokyo", "check_in": _future(30), "check_out": _future(32)}
    args.update(overrides)
    return args


def test_search_resolves_a_city_through_tmc_when_no_google_key_is_set(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route()
    fake = _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert body["search_area"] == "Tokyo"
    body_sent = fake.bodies[SEARCH_PATH][0]
    assert body_sent["latGoogle"] == pytest.approx(35.681143)
    assert body_sent["lngGoogle"] == pytest.approx(139.767208)
    assert body_sent["distance"] == 20
    assert body_sent["pageSize"] == 20
    assert body_sent["checkInDate"] == _future(30)
    assert body_sent["checkOutDate"] == _future(32)
    assert "hotelStars" not in body_sent


def test_search_sorts_by_distance_and_quotes_nightly_and_stay_totals(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route()
    _install(monkeypatch, {loc: loc_resp, search: search_resp})

    hotels = json.loads(tools_hotels.search_hotels(_search_args()))["hotels"]

    assert [hotel["hotel_id"] for hotel in hotels] == [10087697, 10102780, 10233942]
    first = hotels[0]
    assert first["from_nightly_price"] == {"amount": "1.48", "currency": "USD"}
    assert first["stay_total_price"] == {"amount": "2.96", "currency": "USD"}
    assert first["stars"] == 4
    assert (
        first["map_url"]
        == "https://www.google.com/maps/search/?api=1&query=35.667448,139.74734"
    )


def test_google_key_resolves_the_destination_by_geocoding(monkeypatch):
    monkeypatch.setattr(
        _common,
        "secret",
        lambda name: {
            "TMC_APP_KEY": APP_KEY,
            "TMC_APP_SECRET": SECRET,
            geo.GOOGLE_KEY_ENV: "google-key",
        }.get(name, ""),
    )
    monkeypatch.setattr(
        geo,
        "geocode",
        lambda query, language="en": geo.GeoPoint(
            35.6, 139.7, "Tokyo Tower, Tokyo, Japan", "pid"
        ),
    )
    search, search_resp = _search_route()
    loc, loc_resp = _locations_route()
    fake = _install(monkeypatch, {search: search_resp, loc: loc_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args(destination="東京鐵塔")))

    assert body["search_area"] == "Tokyo Tower, Tokyo, Japan"
    assert fake.bodies[SEARCH_PATH][0]["latGoogle"] == 35.6
    assert ("GET", LOCATIONS_PATH) not in fake.requests


def test_unpriced_rows_are_dropped_not_shown_as_free(monkeypatch):
    loc, loc_resp = _locations_route()
    payload = copy.deepcopy(_fixture("tmc_hotel_search.json")["data"])
    payload["hotels"][0]["lowestPrice"] = 0
    search, search_resp = _search_route(payload)
    _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert len(body["hotels"]) == 2
    assert all(Decimal(hotel["from_nightly_price"]["amount"]) > 0 for hotel in body["hotels"])


def test_min_stars_is_sent_as_the_supplier_star_codes(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route()
    fake = _install(monkeypatch, {loc: loc_resp, search: search_resp})

    tools_hotels.search_hotels(_search_args(min_stars=4))

    assert fake.bodies[SEARCH_PATH][0]["hotelStars"] == [19, 29, 39, 49]


def test_empty_search_is_an_ok_result_with_a_hint(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route({"hotels": [], "totalCount": 0})
    _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert body["hotels"] == []
    assert "No hotels found" in body["hint"]


def test_unpriced_hotels_are_not_reported_as_no_hotels(monkeypatch):
    loc, loc_resp = _locations_route()
    hotels = _fixture("tmc_hotel_search.json")["data"]["hotels"]
    search, search_resp = _search_route(
        {"hotels": [_unpriced(hotel) for hotel in hotels], "totalCount": 3, "totalPage": 1}
    )
    _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert body["hotels"] == []
    assert "none had a list price" in body["hint"]
    assert "get_hotel_rooms" in body["hint"]


def test_search_reads_later_pages_until_it_has_priced_hotels(monkeypatch):
    loc, loc_resp = _locations_route()
    hotels = _fixture("tmc_hotel_search.json")["data"]["hotels"]
    first = {"hotels": [_unpriced(hotel) for hotel in hotels], "totalCount": 6, "totalPage": 2}
    second = {"hotels": hotels, "totalCount": 6, "totalPage": 2}
    search = ("POST", SEARCH_PATH)
    fake = _install(
        monkeypatch,
        {
            loc: loc_resp,
            search: [(200, {"code": "0", "data": first}), (200, {"code": "0", "data": second})],
        },
    )

    body = json.loads(tools_hotels.search_hotels(_search_args(max_results=3)))

    assert [request["currentPage"] for request in fake.bodies[SEARCH_PATH]] == [1, 2]
    assert [hotel["hotel_id"] for hotel in body["hotels"]] == [10087697, 10102780, 10233942]


def test_search_stops_reading_pages_once_max_results_are_priced(monkeypatch):
    loc, loc_resp = _locations_route()
    hotels = _fixture("tmc_hotel_search.json")["data"]["hotels"]
    page = {"hotels": hotels, "totalCount": 30, "totalPage": 10}
    search = ("POST", SEARCH_PATH)
    fake = _install(
        monkeypatch, {loc: loc_resp, search: [(200, {"code": "0", "data": page})] * 5}
    )

    tools_hotels.search_hotels(_search_args(max_results=2))

    assert [request["currentPage"] for request in fake.bodies[SEARCH_PATH]] == [1]


def test_search_reads_at_most_three_pages(monkeypatch):
    loc, loc_resp = _locations_route()
    hotels = _fixture("tmc_hotel_search.json")["data"]["hotels"]
    page = {"hotels": [_unpriced(hotel) for hotel in hotels], "totalCount": 90, "totalPage": 30}
    search = ("POST", SEARCH_PATH)
    fake = _install(
        monkeypatch, {loc: loc_resp, search: [(200, {"code": "0", "data": page})] * 10}
    )

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert [request["currentPage"] for request in fake.bodies[SEARCH_PATH]] == [1, 2, 3]
    assert body["hotels"] == []


def test_unknown_city_is_reported_as_not_found(monkeypatch):
    _install(monkeypatch, {("GET", LOCATIONS_PATH): (200, {"code": "0", "data": []})})

    assert (
        _error_code(tools_hotels.search_hotels(_search_args(destination="Nowhere")))
        == "not_found"
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"check_out": None}, "check_out"),
        ({"check_in": _future(-2), "check_out": _future(1)}, "more than a day in the past"),
        ({"check_in": _future(5), "check_out": _future(5)}, "after check_in"),
        ({"check_in": _future(5), "check_out": _future(37)}, "30 nights"),
        ({"check_in": "2026-13-40"}, "yyyy-mm-dd"),
        ({"destination": "  "}, "destination is required"),
        ({"max_results": 11}, "max_results"),
        ({"sort": "stars"}, "sort must be one of"),
    ],
)
def test_bad_arguments_are_refused_before_any_supplier_call(
    monkeypatch, overrides, message
):
    fake = _install(monkeypatch, {})

    result = tools_hotels.search_hotels(_search_args(**overrides))

    assert _error_code(result) == "invalid_arguments"
    assert message in json.loads(result)["error"]
    assert fake.requests == []


def test_check_in_yesterday_is_sent_because_the_server_date_can_run_ahead(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route()
    fake = _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args(check_in=_future(-1), check_out=_future(1))))

    assert "error" not in body
    assert fake.bodies[SEARCH_PATH][0]["checkInDate"] == _future(-1)


def test_hotel_rooms_send_the_stay_party_and_price_each_plan(monkeypatch):
    products = {"code": "0", "data": _fixture("tmc_hotel_products.json")["data"]}
    fake = _install(monkeypatch, {("POST", PRODUCTS_PATH): (200, products)})

    body = json.loads(
        tools_hotels.get_hotel_rooms(
            {
                "hotel_id": 10235214,
                "check_in": _future(30),
                "check_out": _future(32),
                "adults": 2,
                "rooms": 1,
            }
        )
    )

    sent = fake.bodies[PRODUCTS_PATH][0]
    assert sent["hotelId"] == 10235214
    assert sent["roomNum"] == 1
    assert sent["roomGuestNumber"] == {"numberOfAdults": 2, "roomNumber": 1}
    room = body["room_types"][0]
    assert room["room_id"] == 50593980
    first_rate = room["rates"][0]
    assert first_rate["total_price"] == {"amount": "2.96", "currency": "USD"}
    assert first_rate["avg_nightly_price"] == {"amount": "1.48", "currency": "USD"}
    assert first_rate["max_guests"] == 5
    assert "Only rates with a quote_id can be booked" in body["hint"]


def test_hotel_rooms_refuse_a_party_that_does_not_split_evenly(monkeypatch):
    fake = _install(monkeypatch, {})

    result = tools_hotels.get_hotel_rooms(
        {
            "hotel_id": 10235214,
            "check_in": _future(30),
            "check_out": _future(32),
            "adults": 3,
            "rooms": 2,
        }
    )

    assert _error_code(result) == "invalid_arguments"
    assert fake.requests == []


def test_rooms_with_no_price_for_the_party_are_left_out(monkeypatch):
    payload = copy.deepcopy(_fixture("tmc_hotel_products.json")["data"])
    for room in payload:
        for product in room["products"]:
            product["totalSalePrice"] = 0
    _install(
        monkeypatch, {("POST", PRODUCTS_PATH): (200, {"code": "0", "data": payload})}
    )

    body = json.loads(
        tools_hotels.get_hotel_rooms(
            {"hotel_id": 10235214, "check_in": _future(30), "check_out": _future(31)}
        )
    )

    assert body["room_types"] == []
    assert "No rooms with a price" in body["hint"]


def test_hotel_details_are_compact_plain_text(monkeypatch):
    _install(
        monkeypatch,
        {
            ("GET", DETAIL_PATH): (
                200,
                {"code": "0", "data": _fixture("tmc_hotel_detail.json")["data"]},
            )
        },
    )

    body = json.loads(tools_hotels.get_hotel_details({"hotel_id": 10235214}))

    hotel = body["hotel"]
    assert hotel["check_in_time"] == "15:00"
    assert hotel["check_out_time"] == "12:00"
    assert not re.search(r"<[^>]+>", hotel["introduction"])
    assert len(hotel["introduction"]) <= 800
    assert all(name for name in hotel["facilities"])
    assert all(
        not re.search(r"<[^>]+>", policy["text"]) for policy in hotel["policies"]
    )
    assert len(hotel["photos"]) <= 5
    assert "get_hotel_rooms" in body["hint"]
    assert body["note"] == _common.UNTRUSTED_NOTE


def test_unknown_hotel_details_are_not_found(monkeypatch):
    _install(
        monkeypatch,
        {("GET", "/openapi/v1/tmc/hotel/77"): (200, {"code": "0", "data": None})},
    )

    assert _error_code(tools_hotels.get_hotel_details({"hotel_id": 77})) == "not_found"


def test_hotel_tools_are_gated_on_the_tmc_credentials_only():
    names = {spec.name: spec.requires_env for spec in tools_hotels.TOOLS}
    assert names == {
        "search_hotels": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
        "get_hotel_details": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
        "get_hotel_rooms": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
    }


def _rates_for(monkeypatch, *, currency: str | None = None, total: float | None = None) -> list[dict]:
    """The room rates of the fixture hotel, optionally re-priced in another currency."""
    payload = copy.deepcopy(_fixture("tmc_hotel_products.json")["data"])
    for room in payload:
        for product in room["products"]:
            if currency is not None:
                product["currency"] = currency
            if total is not None:
                product["totalSalePrice"] = total
    _install(monkeypatch, {("POST", PRODUCTS_PATH): (200, {"code": "0", "data": payload})})
    body = json.loads(tools_hotels.get_hotel_rooms(
        {"hotel_id": 10235214, "check_in": _future(30), "check_out": _future(32), "adults": 2}))
    return [rate for room in body["room_types"] for rate in room["rates"]]


def test_a_shown_rate_is_remembered_with_what_an_order_needs(monkeypatch):
    first_rate = _rates_for(monkeypatch)[0]

    quote = quotes._quotes[first_rate["quote_id"]]
    assert quote.kind == quotes.HOTEL
    assert quote.owner[1] == "user-a"
    assert quote.facts.hotel_id == 10235214
    assert quote.facts.hotel_name == "Hotel Metropolitan Tokyo Marunouchi"
    assert quote.facts.rate_plan_id == "5bb6fddc94044181aa3e760494f8bf80"
    assert quote.facts.room_id == 50593980
    assert (quote.facts.adults, quote.facts.rooms) == (2, 1)
    assert quote.facts.total.amount == Decimal("2.96")


def test_a_rate_in_another_currency_is_converted_and_marked_approximate(monkeypatch):
    rate = _rates_for(monkeypatch, currency="GBP", total=100)[0]

    expected = fx.to_usd(100, "GBP")
    assert rate["total_price"] == {
        "amount": str(expected.amount), "currency": "USD", "approximate": True,
        "supplier_price": {"amount": "100", "currency": "GBP"}}
    assert quotes._quotes[rate["quote_id"]].facts.total == expected


def test_a_rate_in_a_currency_without_a_rate_is_shown_unconverted_and_cannot_be_ordered(monkeypatch):
    rate = _rates_for(monkeypatch, currency="XYZ", total=100)[0]

    assert rate["total_price"] == {"amount": "100.00", "currency": "XYZ", "unconverted": True}
    assert "quote_id" not in rate
    assert quotes._quotes == {}


def test_hotel_search_prices_are_usd_strings_and_the_names_are_kept_for_rates(monkeypatch):
    loc, loc_resp = _locations_route()
    search, search_resp = _search_route()
    _install(monkeypatch, {loc: loc_resp, search: search_resp})

    body = json.loads(tools_hotels.search_hotels(_search_args()))

    assert all(isinstance(hotel["from_nightly_price"]["amount"], str) for hotel in body["hotels"])
    assert tools_hotels._hotels[body["hotels"][0]["hotel_id"]][0] == body["hotels"][0]["name"]
