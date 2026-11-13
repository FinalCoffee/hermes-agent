"""The travel standard through the hotel and flight tools: what a search leaves out, and what gets no quote.

The supplier is replaced at the client's functions (tmc_client.flight_search and its siblings), so each test states
the fares or rates on sale and nothing leaves the process.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from plugins.accomy_travel import _common, quotes, standard, tmc_client, tools_flights, tools_hotels

DAY = (date.today() + timedelta(days=30)).isoformat()
CHECK_OUT = (date.today() + timedelta(days=32)).isoformat()  # a two-night stay
TOKYO = "3593"


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    quotes._quotes.clear()
    tools_hotels._hotels.clear()
    monkeypatch.setattr(_common, "caller_id", lambda: "user-a")
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    yield
    quotes._quotes.clear()


# ── the rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("city_code", ["2114", "2734", "2621"])
def test_london_paris_and_new_york_have_the_higher_nightly_budget(city_code):
    assert standard.nightly_budget(city_code) == Decimal(300)


@pytest.mark.parametrize("city_code", [TOKYO, "", None])
def test_every_other_city_and_an_unknown_one_have_the_general_nightly_budget(city_code):
    assert standard.nightly_budget(city_code) == Decimal(200)


@pytest.mark.parametrize(
    "cabin, minutes, allowed",
    [
        ("economy", 60, True),
        ("economy", None, True),
        ("premium_economy", 479, False),
        ("premium_economy", 480, True),
        ("business", 479, False),
        ("business", 480, True),
        ("business", None, False),
        ("first", 900, False),
    ],
)
def test_premium_cabins_need_eight_hours_of_flying_time_and_first_class_is_never_allowed(cabin, minutes, allowed):
    assert standard.cabin_allowed(cabin, minutes) is allowed


def test_the_fare_limit_is_seventy_percent_above_the_cheapest_fare_rounded_down_to_the_cent():
    assert standard.fare_limit(Decimal(246)) == Decimal("418.20")
    assert standard.fare_limit(Decimal("323.45")) == Decimal("549.86")


# ── flights ──────────────────────────────────────────────────────────────────


def _flight(key: str, price: float, durations: list, cabins: list | None = None) -> tuple[dict, dict]:
    """One flight on sale with one fare, and its segments: one segment per duration, HKG to SIN."""
    stops = ["HKG", "BKK", "KUL", "SIN"]
    airports = [*stops[: len(durations)], "SIN"] if len(durations) > 1 else ["HKG", "SIN"]
    segments = {
        f"{key}-{index}": {
            "airlineCode": "CX", "airlineName": "Cathay Pacific", "flightNumber": f"CX{100 + index}",
            "departureAirport": airports[index], "arrivalAirport": airports[index + 1],
            "departureDate": DAY, "departureTime": "08:00", "arrivalDate": DAY, "arrivalTime": "12:00",
            "flightDuration": duration,
        }
        for index, duration in enumerate(durations)
    }
    flight = {
        "journeys": [{
            "origin": "HKG", "destination": "SIN", "originDate": DAY, "originTime": "08:00",
            # Far longer than any flying time here: the journey's own time includes the wait at each stop.
            "journeysTime": "30h", "transferCount": len(durations) - 1, "airSegmentList": list(segments),
        }],
        "offerPriceList": [{
            "totalSalePrice": price, "currency": "USD", "priceKey": f"price-key-{key}",
            "cabinClass": cabins if cabins is not None else ["economy"] * len(durations),
        }],
    }
    return flight, segments


@pytest.fixture
def flights(monkeypatch):
    """search(*flights, **args) runs search_flights against those fares; .sent holds the request bodies."""
    sent: list[dict] = []
    on_sale: dict = {}

    def flight_search(body):
        sent.append(body)
        return on_sale

    def search(*found, **args):
        on_sale.clear()
        on_sale.update({"flight": [flight for flight, _ in found], "segments": {}})
        for _, segments in found:
            on_sale["segments"].update(segments)
        return json.loads(tools_flights.search_flights(
            {"origin": "HKG", "destination": "SIN", "depart_date": DAY, "max_results": 10, **args}))

    monkeypatch.setattr(tmc_client, "flight_search", flight_search)
    monkeypatch.setattr(tmc_client, "airport_rows", lambda code: [{"cityCode": code, "countryCode": code}])
    search.sent = sent
    return search


def test_a_fare_more_than_seventy_percent_above_the_cheapest_is_left_out_and_gets_no_quote(flights):
    body = flights(_flight("a", 100, ["3h"]), _flight("b", 170, ["3h"]), _flight("c", 170.01, ["3h"]))

    assert [flight["price"]["amount"] for flight in body["flights"]] == ["100.00", "170.00"]
    assert body["total_offers"] == 2
    assert body["outside_travel_standard"] == 1
    assert body["fare_limit"] == {"amount": "170.00", "currency": "USD"}
    assert {quote.facts.price_key for quote in quotes._quotes.values()} == {"price-key-a", "price-key-b"}
    assert "outside the travel standard" in body["hint"]


@pytest.mark.parametrize("cabin", ["premium_economy", "business"])
def test_a_premium_cabin_is_left_out_under_eight_hours_of_flying_and_offered_from_eight(flights, cabin):
    short = flights(_flight("a", 900, ["7h 59m"], [cabin]), cabin=cabin)
    long = flights(_flight("b", 900, ["8h"], [cabin]), cabin=cabin)

    assert short["flights"] == [] and short["outside_travel_standard"] == 1
    assert "search economy" in short["hint"]
    assert [flight["cabin"] for flight in long["flights"]] == [cabin]
    assert long["outside_travel_standard"] == 0
    assert [quote.facts.price_key for quote in quotes._quotes.values()] == ["price-key-b"]


def test_time_on_the_ground_at_a_stop_is_not_flying_time(flights):
    body = flights(
        _flight("a", 900, ["4h", "3h 50m"], ["business", "business"]),
        _flight("b", 900, ["4h 20m", "3h 50m"], ["business", "business"]),
        cabin="business",
    )

    assert [flight["flying_time"] for flight in body["flights"]] == ["8h10m"]
    assert body["outside_travel_standard"] == 1


def test_a_flight_with_no_duration_from_the_supplier_counts_as_a_short_one(flights):
    body = flights(_flight("a", 900, [None], ["business"]), cabin="business")

    assert body["flights"] == [] and body["outside_travel_standard"] == 1


def test_a_short_flight_sold_partly_in_business_is_left_out_of_an_economy_search(flights):
    body = flights(_flight("a", 100, ["2h", "3h"], ["business", "economy"]), _flight("b", 120, ["3h"]))

    assert [flight["price"]["amount"] for flight in body["flights"]] == ["120.00"]
    assert body["outside_travel_standard"] == 1


def test_first_class_is_refused_before_any_search_and_left_out_when_the_supplier_returns_it(flights):
    refused = flights(cabin="first")
    assert refused["code"] == "invalid_arguments" and flights.sent == []

    body = flights(_flight("a", 5000, ["12h"], ["first"]), cabin="business")

    assert body["flights"] == [] and body["outside_travel_standard"] == 1


def test_the_searched_cabin_is_sent_to_the_supplier_and_economy_is_the_default(flights):
    flights()
    flights(cabin="premium_economy")

    assert [body["cabinClass"] for body in flights.sent] == ["economy", "premium_economy"]


def test_the_fare_rule_measures_a_business_search_against_its_own_cheapest_fare(flights):
    body = flights(
        _flight("a", 2000, ["12h"], ["business"]),
        _flight("b", 3400, ["12h"], ["business"]),
        _flight("c", 3400.01, ["12h"], ["business"]),
        cabin="business",
    )

    assert [flight["price"]["amount"] for flight in body["flights"]] == ["2000.00", "3400.00"]
    assert body["fare_limit"]["amount"] == "3400.00"


def test_the_cheapest_fare_is_taken_from_the_fares_the_cabin_rule_allows(flights):
    body = flights(
        _flight("short", 100, ["3h"], ["business"]),
        _flight("a", 200, ["12h"], ["business"]),
        _flight("b", 340, ["12h"], ["business"]),
        _flight("c", 340.01, ["12h"], ["business"]),
        cabin="business",
    )

    assert [flight["price"]["amount"] for flight in body["flights"]] == ["200.00", "340.00"]
    assert body["fare_limit"]["amount"] == "340.00"
    assert body["outside_travel_standard"] == 2


def _verified_at(monkeypatch, price: float) -> None:
    monkeypatch.setattr(
        tmc_client, "flight_verify", lambda price_key: {"offerData": {"totalSalePrice": price, "currency": "USD"}})


def test_a_fare_that_verifies_above_its_searchs_limit_loses_its_quote(flights, monkeypatch):
    dearer = flights(_flight("a", 100, ["3h"]), _flight("b", 150, ["3h"]))["flights"][1]
    _verified_at(monkeypatch, 170.01)

    body = json.loads(tools_flights.get_flight_offer({"quote_id": dearer["quote_id"]}))
    again = json.loads(tools_flights.get_flight_offer({"quote_id": dearer["quote_id"]}))

    assert body["outside_travel_standard"] is True and "quote_id" not in body
    assert body["fare_limit"] == {"amount": "170.00", "currency": "USD"}
    assert again["code"] == "quote_unknown"


def test_a_fare_that_verifies_within_the_limit_keeps_its_quote_at_the_new_price(flights, monkeypatch):
    dearer = flights(_flight("a", 100, ["3h"]), _flight("b", 150, ["3h"]))["flights"][1]
    _verified_at(monkeypatch, 170)

    body = json.loads(tools_flights.get_flight_offer({"quote_id": dearer["quote_id"]}))

    assert body["quote_id"] == dearer["quote_id"] and "outside_travel_standard" not in body
    assert quotes.lookup(dearer["quote_id"], quotes.FLIGHT).facts.total.amount == Decimal("170.00")


# ── hotels ───────────────────────────────────────────────────────────────────


def _hotel(hotel_id: int, nightly: float, city_code: str = TOKYO) -> dict:
    return {
        "hotelId": hotel_id, "hotelName": f"Hotel {hotel_id}", "cityCode": city_code, "lowestPrice": nightly,
        "saleCurrencyCode": "USD", "latGoogle": 35.68, "lngGoogle": 139.76, "distance": float(hotel_id),
    }


@pytest.fixture
def hotels(monkeypatch):
    """search(*pages) runs search_hotels against those result pages; .pages_read holds the page numbers asked."""
    pages_read: list[int] = []
    pages: list[list[dict]] = []

    def hotel_search(*, page, **_stay):
        pages_read.append(page)
        return {"hotels": pages[page - 1], "totalCount": sum(map(len, pages)), "totalPage": len(pages)}

    def search(*found):
        pages[:] = found
        return json.loads(tools_hotels.search_hotels(
            {"destination": "Tokyo", "check_in": DAY, "check_out": CHECK_OUT, "max_results": 10}))

    city = {"dataType": 2, "destinationName": [{"name": "Tokyo", "latGoogle": 35.68, "lngGoogle": 139.76}]}
    monkeypatch.setattr(_common, "secret", lambda name: "")
    monkeypatch.setattr(tmc_client, "hotel_locations", lambda keyword: [city])
    monkeypatch.setattr(tmc_client, "hotel_search", hotel_search)
    search.pages_read = pages_read
    return search


def test_a_hotel_above_two_hundred_dollars_a_night_is_left_out(hotels):
    body = hotels([_hotel(1, 150), _hotel(2, 200), _hotel(3, 200.01)])

    assert [hotel["hotel_id"] for hotel in body["hotels"]] == [1, 2]
    assert body["outside_travel_standard"] == 1
    assert "travel standard" in body["hint"]


@pytest.mark.parametrize("city_code", list(standard.HOTEL_BUDGET_CITIES))
def test_a_hotel_in_london_paris_or_new_york_may_cost_three_hundred_a_night(hotels, city_code):
    body = hotels([_hotel(1, 300, city_code), _hotel(2, 300.01, city_code), _hotel(3, 250)])

    assert [hotel["hotel_id"] for hotel in body["hotels"]] == [1]
    assert body["outside_travel_standard"] == 2


def test_a_search_whose_every_hotel_is_over_budget_says_so_instead_of_no_hotels(hotels):
    body = hotels([_hotel(1, 450), _hotel(2, 520)])

    assert body["hotels"] == [] and body["outside_travel_standard"] == 2
    assert "more per night than the travel standard allows" in body["hint"]


def test_a_search_that_stopped_before_the_last_page_says_further_hotels_were_not_checked(hotels):
    body = hotels([_hotel(1, 450)], [_hotel(2, 450)], [_hotel(3, 450)], [_hotel(4, 120)])

    assert hotels.pages_read == [1, 2, 3]
    assert body["hotels"] == [] and body["outside_travel_standard"] == 3
    assert "further away were not checked" in body["hint"]


def test_search_reads_the_next_page_when_a_page_holds_only_hotels_over_budget(hotels):
    body = hotels([_hotel(1, 450), _hotel(2, 520)], [_hotel(3, 180)])

    assert [hotel["hotel_id"] for hotel in body["hotels"]] == [3]
    assert hotels.pages_read == [1, 2]


def _rate(plan: str, total: float, nights: tuple = ()) -> dict:
    """A rate plan for the stay; *nights* are the prices of one room for each night, when the supplier lists them."""
    items = [{"salePrice": price, "currency": "USD"} for price in nights]
    return {"ratePlanId": plan, "ratePlanName": plan, "totalSalePrice": total, "currency": "USD", "priceItems": items}


@pytest.fixture
def rooms(monkeypatch):
    """rates(*rates, city_code=..., rooms=1) runs get_hotel_rooms for a two-night stay at hotel 77."""

    def rates(*on_sale, city_code: str = TOKYO, rooms: int = 1, name: str | None = "Hotel 77"):
        monkeypatch.setattr(tmc_client, "hotel_detail", lambda hotel_id: {"hotelName": name, "cityCode": city_code})
        monkeypatch.setattr(
            tmc_client, "hotel_products",
            lambda **_stay: [{"roomId": 1, "roomName": "Deluxe", "products": list(on_sale)}])
        return json.loads(tools_hotels.get_hotel_rooms(
            {"hotel_id": 77, "check_in": DAY, "check_out": CHECK_OUT, "adults": 2 * rooms, "rooms": rooms}))

    return rates


def _plans(body: dict) -> list[str]:
    return [rate["rate_plan"] for room in body["room_types"] for rate in room["rates"]]


def test_a_rate_averaging_over_the_budget_per_room_per_night_is_left_out_and_gets_no_quote(rooms):
    body = rooms(_rate("within", 400), _rate("over", 400.02))

    assert _plans(body) == ["within"]
    assert body["nightly_budget"] == {"amount": "200.00", "currency": "USD"}
    assert body["outside_travel_standard"] == 1
    assert [quote.facts.rate_plan_id for quote in quotes._quotes.values()] == ["within"]


def test_a_rate_with_one_night_over_the_budget_is_left_out_even_when_the_stay_averages_within_it(rooms):
    body = rooms(_rate("even", 400, (200, 200)), _rate("uneven", 400, (150, 250)))

    assert _plans(body) == ["even"]
    assert [quote.facts.rate_plan_id for quote in quotes._quotes.values()] == ["even"]


def test_a_stay_total_that_averages_over_the_budget_is_left_out_though_each_listed_night_is_within(rooms):
    body = rooms(_rate("with tax", 410, (190, 190)))

    assert body["room_types"] == [] and body["outside_travel_standard"] == 1


def test_the_budget_is_per_room_so_two_rooms_allow_twice_the_stay_total(rooms):
    body = rooms(_rate("within", 800), _rate("over", 800.04), rooms=2)

    assert _plans(body) == ["within"]


def test_a_hotels_budget_follows_its_city_even_when_no_search_showed_the_hotel(rooms):
    body = rooms(_rate("within", 600), _rate("over", 600.02), city_code="2734")

    assert _plans(body) == ["within"]
    assert body["nightly_budget"]["amount"] == "300.00"


def test_a_hotel_whose_detail_page_has_no_name_still_gets_its_citys_budget(rooms):
    body = rooms(_rate("within", 500, (250, 250)), city_code="2114", name=None)

    assert _plans(body) == ["within"]
    assert body["nightly_budget"]["amount"] == "300.00"


def test_a_hotel_whose_every_rate_is_over_budget_says_so_instead_of_no_rooms(rooms):
    body = rooms(_rate("over", 900))

    assert body["room_types"] == [] and body["outside_travel_standard"] == 1
    assert "more per room per night than the travel standard" in body["hint"]
