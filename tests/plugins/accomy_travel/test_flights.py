"""Flight tools through their handlers: airport lookup, one-way search, offer ids and live verification.

Fixtures are trimmed real TMC answers (tests/plugins/accomy_travel/fixtures/tmc_flight_*.json and tmc_airports.json).
The supplier is a fake transport, so nothing leaves the process.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest

from decimal import Decimal

from plugins.accomy_travel import _common, quotes, tmc_client, tools_flights

FIXTURES = Path(__file__).parent / "fixtures"
APP_KEY = "app-key-for-tests"
SECRET = "secret-for-tests"
TOKEN_PATH = "/openapi/v1/tmc/auth/token"
AIRPORTS_PATH = "/openapi/v1/tmc/dict/airports"
SEARCH_PATH = "/openapi/v1/tmc/flight/search"
VERIFY_PATH = "/openapi/v1/tmc/flight/verify"
QUOTE_ID_RE = re.compile(r"Q[0-9A-F]{8}")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _airport_row(code: str, country: str, name: str) -> dict:
    return {
        "cityCode": code,
        "cityNameEn": name,
        "countryCode": country,
        "airports": [
            {
                "airportCode": code,
                "airportNameEn": f"{name} International Airport",
                "countryCode": country,
                "isActive": 1,
            }
        ],
    }


KNOWN_AIRPORTS = {
    "HKG": _airport_row("HKG", "HK", "Hong Kong"),
    "SIN": _airport_row("SIN", "SG", "Singapore"),
    "NRT": _airport_row("NRT", "JP", "Tokyo"),
    "HND": _airport_row("HND", "JP", "Tokyo"),
}


class FakeTmc:
    def __init__(self, routes: dict) -> None:
        self.routes = {
            ("POST", TOKEN_PATH): (200, {"code": "0", "data": {"token": "session-1"}}),
            **routes,
        }
        self.requests: list[tuple[str, str]] = []
        self.bodies: dict[str, list] = {}
        self.urls: list[str] = []

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
        self.urls.append(url)
        if content:
            self.bodies.setdefault(path, []).append(json.loads(content))
        responder = self.routes[(method, path)]
        return responder(url) if callable(responder) else responder


def _airports_responder(url: str) -> tuple[int, dict]:
    keyword = unquote(url.split("keyword=", 1)[1]).upper()
    rows = [row for code, row in KNOWN_AIRPORTS.items() if code == keyword]
    return 200, {"code": "0", "data": rows}


@pytest.fixture(autouse=True)
def _flight_state(monkeypatch):
    tmc_client._tokens.clear()
    tmc_client._airport_cache.clear()
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
    quotes._quotes.clear()


def _install(monkeypatch, routes: dict | None = None) -> FakeTmc:
    base = {
        ("GET", AIRPORTS_PATH): _airports_responder,
        ("POST", SEARCH_PATH): (
            200,
            {"code": "0", "data": _fixture("tmc_flight_search.json")["data"]},
        ),
    }
    fake = FakeTmc({**base, **(routes or {})})
    monkeypatch.setattr(_common, "request_json", fake)
    return fake


def _days(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def _search_args(**overrides) -> dict:
    args = {"origin": "HKG", "destination": "SIN", "depart_date": _days(30)}
    args.update(overrides)
    return args


def _error_code(result: str) -> str:
    body = json.loads(result)
    assert "error" in body, body
    return body["code"]


def _fixture_offers() -> list[dict]:
    """Every fixture offer as (flight, offer), keeping the fixture's own order."""
    pairs = []
    for flight in _fixture("tmc_flight_search.json")["data"]["flight"]:
        for offer in flight["offerPriceList"]:
            pairs.append((flight, offer))
    return pairs


def test_airport_lookup_lists_active_airports_by_city_in_any_language(monkeypatch):
    fake = _install(
        monkeypatch, {("GET", AIRPORTS_PATH): (200, _fixture("tmc_airports.json"))}
    )

    body = json.loads(tools_flights.search_airports({"keyword": "台北"}))

    assert "keyword=" in fake.urls[-1]
    city = body["cities"][0]
    assert city["city_code"] == "TPE"
    assert [airport["airport_code"] for airport in city["airports"]] == ["TSA", "TPE"]
    assert city["airports"][1]["name_en"] == "Taiwan Taoyuan International Airport"
    assert "isActive" not in json.dumps(body)


def test_depart_yesterday_is_sent_because_the_server_date_can_run_ahead(monkeypatch):
    fake = _install(monkeypatch)

    result = json.loads(tools_flights.search_flights(_search_args(depart_date=_days(-1))))

    assert "error" not in result
    assert fake.bodies[SEARCH_PATH][0]["journeys"][0]["departureDate"] == _days(-1)


def test_one_way_search_sends_airport_types_and_the_international_flag(monkeypatch):
    fake = _install(monkeypatch)

    tools_flights.search_flights(_search_args())

    sent = fake.bodies[SEARCH_PATH][0]
    assert sent["tripType"] == 1
    assert sent["cabinClass"] == "economy"
    assert sent["adultNum"] == 1
    assert sent["transferNumber"] == 0
    assert sent["language"] == "en-US"
    journey = sent["journeys"][0]
    assert journey["origin"] == "HKG"
    assert journey["destination"] == "SIN"
    assert journey["originType"] == 2
    assert journey["destinationType"] == 2
    assert journey["isInternational"] == 1
    assert journey["departureDate"] == _days(30)


def test_a_domestic_route_is_sent_as_not_international(monkeypatch):
    fake = _install(monkeypatch)

    tools_flights.search_flights(_search_args(origin="NRT", destination="HND"))

    assert fake.bodies[SEARCH_PATH][0]["journeys"][0]["isInternational"] == 0


def test_direct_only_sends_transfer_number_one(monkeypatch):
    fake = _install(monkeypatch)

    tools_flights.search_flights(_search_args(direct_only=True))

    assert fake.bodies[SEARCH_PATH][0]["transferNumber"] == 1


def test_results_are_sorted_cheapest_first_and_capped(monkeypatch):
    _install(monkeypatch)

    body = json.loads(tools_flights.search_flights(_search_args(max_results=2)))

    prices = [Decimal(flight["price"]["amount"]) for flight in body["flights"]]
    expected = sorted(Decimal(str(offer["totalSalePrice"])) for _flight, offer in _fixture_offers())[:2]
    assert prices == expected
    assert len(body["flights"]) == 2
    assert body["total_offers"] == len(_fixture_offers())


def test_duration_sort_orders_by_flight_time(monkeypatch):
    _install(monkeypatch)

    body = json.loads(
        tools_flights.search_flights(_search_args(sort="duration", max_results=10))
    )

    minutes = [tools_flights._minutes(flight["duration"]) for flight in body["flights"]]
    assert minutes == sorted(minutes)


def test_each_offer_summarises_its_segments_and_local_times(monkeypatch):
    _install(monkeypatch)

    first = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]

    assert first["depart"]["airport"] == "HKG"
    assert first["depart"]["local"]
    assert first["arrive"]["airport"] == "SIN"
    assert first["stops"] == len(first["segments"]) - 1
    segment = first["segments"][0]
    assert segment["airline"]
    assert re.fullmatch(r"[A-Z]{2}\d{3,4}", segment["flight"])
    assert segment["from"] == "HKG"
    assert segment["depart_local"]
    assert segment["duration"]
    assert first["baggage"]["carry_on"].endswith("KG")


def test_policy_blocked_flights_and_age_limited_fares_are_dropped(monkeypatch):
    data = copy.deepcopy(_fixture("tmc_flight_search.json")["data"])
    data["flight"][0]["policyBlocked"] = 1
    data["flight"][1]["offerPriceList"][0]["salesLimit"] = {"ageLimit": ["adult only"]}
    _install(monkeypatch, {("POST", SEARCH_PATH): (200, {"code": "0", "data": data})})

    body = json.loads(tools_flights.search_flights(_search_args(max_results=10)))

    blocked = len(data["flight"][0]["offerPriceList"])
    restricted = 1
    assert body["total_offers"] == len(_fixture_offers()) - blocked - restricted


def test_no_inventory_is_an_empty_result_with_a_hint_not_an_error(monkeypatch):
    _install(monkeypatch, {("POST", SEARCH_PATH): (200, {"code": "0", "data": None})})

    body = json.loads(tools_flights.search_flights(_search_args()))

    assert body["flights"] == []
    assert "No flights are on sale" in body["hint"]


def test_quote_ids_are_short_and_the_price_key_never_reaches_the_model(monkeypatch):
    _install(monkeypatch)

    result = tools_flights.search_flights(_search_args(max_results=3))
    body = json.loads(result)

    assert all(QUOTE_ID_RE.fullmatch(flight["quote_id"]) for flight in body["flights"])
    assert "priceKey" not in result
    assert body["note"] == _common.UNTRUSTED_NOTE


def test_unknown_code_is_refused_with_a_pointer_to_airport_search_and_no_search_is_sent(
    monkeypatch,
):
    fake = _install(monkeypatch)

    result = tools_flights.search_flights(_search_args(origin="ZZZ"))

    assert _error_code(result) == "invalid_arguments"
    assert "search_airports" in json.loads(result)["error"]
    assert ("POST", SEARCH_PATH) not in fake.requests


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"destination": "HKG"}, "must differ"),
        ({"origin": "HK1"}, "3-letter IATA"),
        ({"depart_date": _days(-2)}, "more than a day in the past"),
        ({"depart_date": "2026/12/24"}, "yyyy-mm-dd"),
        ({"adults": 10}, "adults must be from 1 to 9"),
        ({"sort": "cheapest"}, "sort must be one of"),
        ({"max_results": 0}, "max_results must be from 1 to 10"),
    ],
)
def test_bad_arguments_are_refused_before_any_search(monkeypatch, overrides, message):
    fake = _install(monkeypatch)

    result = tools_flights.search_flights(_search_args(**overrides))

    assert _error_code(result) == "invalid_arguments"
    assert message in json.loads(result)["error"]
    assert ("POST", SEARCH_PATH) not in fake.requests


def test_get_flight_offer_verifies_the_cheapest_searched_fare(monkeypatch):
    fake = _install(
        monkeypatch,
        {
            ("POST", VERIFY_PATH): (
                200,
                {"code": "0", "data": _fixture("tmc_flight_verify.json")["data"]},
            )
        },
    )
    first = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]

    tools_flights.get_flight_offer({"quote_id": first["quote_id"]})

    cheapest = min(_fixture_offers(), key=lambda pair: pair[1]["totalSalePrice"])[1]
    assert fake.bodies[VERIFY_PATH][0]["priceKey"] == cheapest["priceKey"]


def test_quote_ids_are_bound_to_the_caller_who_searched(monkeypatch):
    _install(
        monkeypatch,
        {
            ("POST", VERIFY_PATH): (
                200,
                {"code": "0", "data": _fixture("tmc_flight_verify.json")["data"]},
            )
        },
    )
    quote_id = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]["quote_id"]
    monkeypatch.setattr(_common, "caller_id", lambda: "user-b")

    result = tools_flights.get_flight_offer({"quote_id": quote_id})

    assert _error_code(result) == "quote_unknown"


def test_quote_ids_expire_after_thirty_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(quotes.time, "monotonic", lambda: clock[0])
    _install(
        monkeypatch,
        {
            ("POST", VERIFY_PATH): (
                200,
                {"code": "0", "data": _fixture("tmc_flight_verify.json")["data"]},
            )
        },
    )
    quote_id = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]["quote_id"]

    clock[0] = 1000.0 + 29 * 60
    assert "price" in json.loads(tools_flights.get_flight_offer({"quote_id": quote_id}))

    clock[0] = 1000.0 + 31 * 60
    assert (
        _error_code(tools_flights.get_flight_offer({"quote_id": quote_id}))
        == "quote_unknown"
    )


def test_verify_renders_live_price_rules_and_a_price_change(monkeypatch):
    verify = copy.deepcopy(_fixture("tmc_flight_verify.json")["data"])
    verify["offerData"]["totalSalePrice"] = 900
    _install(monkeypatch, {("POST", VERIFY_PATH): (200, {"code": "0", "data": verify})})
    searched = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]

    body = json.loads(
        tools_flights.get_flight_offer({"quote_id": searched["quote_id"]})
    )

    assert body["price"] == {"amount": "900.00", "currency": "USD"}
    assert body["searched_price"]["amount"] == searched["price"]["amount"]
    assert "price changed" in body["hint"]
    windows = {(item["when"], item["fee"]["amount"]) for item in body["refund_rules"]}
    assert ("before departure", "176.00") in windows
    assert ("after departure", "376.00") in windows
    assert len(body["refund_rules"]) == len(windows)
    assert body["baggage"]["checked"].endswith("KG")
    assert not re.search(r"<[^>]+>", body["booking_notice"])
    assert body["note"] == _common.UNTRUSTED_NOTE


def test_unchanged_price_is_not_repeated_as_a_change(monkeypatch):
    _install(monkeypatch)
    searched = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]
    verify = copy.deepcopy(_fixture("tmc_flight_verify.json")["data"])
    verify["offerData"]["totalSalePrice"] = float(searched["price"]["amount"])
    _install(monkeypatch, {("POST", VERIFY_PATH): (200, {"code": "0", "data": verify})})

    body = json.loads(
        tools_flights.get_flight_offer({"quote_id": searched["quote_id"]})
    )

    assert "searched_price" not in body
    assert "price changed" not in body["hint"]


def test_a_fare_the_supplier_no_longer_sells_is_offer_expired(monkeypatch):
    _install(
        monkeypatch,
        {
            ("POST", VERIFY_PATH): (
                200,
                {"code": "VERIFY_PRICE_FAILED", "message": "Invalid price key"},
            )
        },
    )
    quote_id = json.loads(tools_flights.search_flights(_search_args(max_results=1)))[
        "flights"
    ][0]["quote_id"]

    assert (
        _error_code(tools_flights.get_flight_offer({"quote_id": quote_id}))
        == "quote_unknown"
    )


def test_quote_id_must_look_like_an_quote_id(monkeypatch):
    fake = _install(monkeypatch)

    assert (
        _error_code(tools_flights.get_flight_offer({"quote_id": "abc"}))
        == "invalid_arguments"
    )
    assert ("POST", VERIFY_PATH) not in fake.requests


def test_flight_tools_are_gated_on_the_tmc_credentials_only():
    names = {spec.name: spec.requires_env for spec in tools_flights.TOOLS}
    assert names == {
        "search_airports": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
        "search_flights": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
        "get_flight_offer": (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV),
    }


def _flight_data(currency: str | None = None) -> dict:
    data = copy.deepcopy(_fixture("tmc_flight_search.json")["data"])
    if currency is not None:
        for flight in data["flight"]:
            for offer in flight["offerPriceList"]:
                offer["currency"] = currency
    return data


def test_a_shown_fare_is_remembered_with_its_legs_party_and_usd_total(monkeypatch):
    _install(monkeypatch)

    first = json.loads(tools_flights.search_flights(_search_args(max_results=1, adults=2)))["flights"][0]

    quote = quotes._quotes[first["quote_id"]]
    assert quote.kind == quotes.FLIGHT
    assert quote.owner[1] == "user-a"
    fare = quote.facts
    assert fare.adults == 2
    assert fare.total.amount == Decimal(first["price"]["amount"])
    assert [leg.carrier for leg in fare.legs] == [segment["flight"][:2] for segment in first["segments"]]
    assert fare.legs[0].origin == "HKG" and fare.legs[0].depart_local.strftime("%Y-%m-%d %H:%M") == first["depart"]["local"]
    assert fare.price_key  # the supplier's key, kept for verification and never shown


def test_a_fare_in_a_converted_currency_is_approximate_and_still_orderable(monkeypatch):
    _install(monkeypatch, {("POST", SEARCH_PATH): (200, {"code": "0", "data": _flight_data("GBP")})})

    first = json.loads(tools_flights.search_flights(_search_args(max_results=1)))["flights"][0]

    assert first["price"]["approximate"] is True
    assert first["price"]["supplier_price"]["currency"] == "GBP"
    assert "quote_id" in first


def test_a_fare_in_a_currency_without_a_rate_is_unconverted_and_cannot_be_ordered(monkeypatch):
    _install(monkeypatch, {("POST", SEARCH_PATH): (200, {"code": "0", "data": _flight_data("XYZ")})})

    first = json.loads(tools_flights.search_flights(_search_args(max_results=1)))["flights"][0]

    assert first["price"]["unconverted"] is True
    assert "quote_id" not in first
