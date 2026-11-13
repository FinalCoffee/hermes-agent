"""Round trips and multi-city trips through the flight tools: one search of the whole trip, answered leg by leg.

Fixtures are trimmed real TMC answers for Taipei to Tokyo and back: the flights of each leg
(tmc_flight_round_trip_leg1.json, tmc_flight_round_trip_leg2.json), the fares of the finished itinerary
(tmc_flight_trip_fares.json) and its price check (tmc_flight_trip_verify.json). The supplier is a fake transport
that answers a search by the leg it is asked for, so nothing leaves the process.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import pytest

from plugins.accomy_travel import _common, quotes, tmc_client, tools_flights

FIXTURES = Path(__file__).parent / "fixtures"
PREFIX = tmc_client.BASE_URL + tmc_client.API_PREFIX
SEARCH, FARES, VERIFY = "/flight/search", "/flight/cabin", "/flight/verify"
ID_RE = re.compile(r"Q[0-9A-F]{8}")
COUNTRIES = {"TPE": "TW", "KHH": "TW", "TYO": "JP", "OSA": "JP", "SEL": "KR"}
STALE = {"code": "VERIFY_PRICE_FAILED", "message": "Invalid price key"}


def _data(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"]


class Supplier:
    """TMC as the flight tools see it. A search is answered with ``legs[n]``, n being how many flights the
    request says were picked already."""

    def __init__(self, legs: list, fares=None, verify=None) -> None:
        self.legs = legs
        self.fares = _data("tmc_flight_trip_fares.json") if fares is None else fares
        self.verify = verify or (lambda key: {"code": "0", "data": _data("tmc_flight_trip_verify.json")})
        self.sent: list[tuple[str, dict]] = []

    def bodies(self, path: str) -> list[dict]:
        return [body for sent_path, body in self.sent if sent_path == path]

    def __call__(self, method, url, *, headers=None, params=None, json_body=None, content=None, timeout=None):
        path = url.split("?", 1)[0].removeprefix(PREFIX)
        body = json.loads(content) if content else {}
        self.sent.append((path, body))
        if path == "/auth/token":
            return 200, {"code": "0", "data": {"token": "session-1"}}
        if path == "/dict/airports":
            code = unquote(url.split("keyword=", 1)[1]).upper()
            row = {"cityCode": code, "countryCode": COUNTRIES[code],
                   "airports": [{"airportCode": code, "countryCode": COUNTRIES[code], "isActive": 1}]}
            return 200, {"code": "0", "data": [row] if code in COUNTRIES else []}
        if path == SEARCH:
            return 200, {"code": "0", "data": self.legs[len(body.get("journeyId") or [])]}
        if path == FARES:
            return 200, {"code": "0", "data": self.fares}
        if path == VERIFY:
            return 200, self.verify(body["priceKey"])
        raise AssertionError(f"the flight tools sent an unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    tmc_client._tokens.clear()
    tmc_client._airport_cache.clear()
    quotes._quotes.clear()
    monkeypatch.setattr(
        _common, "secret", lambda name: {"TMC_APP_KEY": "app-key", "TMC_APP_SECRET": "secret"}.get(name, ""))
    monkeypatch.setattr(_common, "caller_id", lambda: "user-a")
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    yield
    tmc_client._tokens.clear()
    quotes._quotes.clear()


def _supplier(monkeypatch, legs=None, **answers) -> Supplier:
    supplier = Supplier(
        [_data("tmc_flight_round_trip_leg1.json"), _data("tmc_flight_round_trip_leg2.json")] if legs is None else legs,
        **answers)
    monkeypatch.setattr(_common, "request_json", supplier)
    return supplier


def _days(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def _round_trip(**overrides) -> dict:
    args = {"origin": "TPE", "destination": "TYO", "depart_date": _days(30), "return_date": _days(34)}
    args.update(overrides)
    return json.loads(tools_flights.search_flights(args))


def _flight(body: dict, number: str) -> dict:
    """The row of the flight with that flight number."""
    return next(row for row in body["flights"] if row["segments"][0]["flight"] == number)


def _continue(option_id: str, **more) -> dict:
    return json.loads(tools_flights.search_flights({"continue_from": option_id, **more}))


def _error(result: str) -> tuple[str, str]:
    body = json.loads(result)
    assert "error" in body, body
    return body["code"], body["error"]


def _trip_quote(monkeypatch, **answers) -> tuple[Supplier, dict]:
    """A round trip searched to its end: the supplier, and the row of the cheapest flight back."""
    supplier = _supplier(monkeypatch, **answers)
    back = _continue(_flight(_round_trip(), "TR0866")["option_id"])
    return supplier, _flight(back, "GK0013")


# ── the search of the whole trip ─────────────────────────────────────────────


def test_a_round_trip_is_one_search_of_both_directions(monkeypatch):
    supplier = _supplier(monkeypatch)

    body = _round_trip()

    (sent,) = supplier.bodies(SEARCH)
    assert sent["tripType"] == 2
    assert [(leg["origin"], leg["destination"], leg["departureDate"]) for leg in sent["journeys"]] == [
        ("TPE", "TYO", _days(30)), ("TYO", "TPE", _days(34))]
    assert "journeyId" not in sent
    assert (body["trip"], body["leg"], body["legs"], body["route"]) == ("round_trip", 1, 2, "TPE-TYO")


def test_a_flight_of_an_earlier_leg_carries_the_trip_on_and_is_no_fare(monkeypatch):
    _supplier(monkeypatch)

    body = _round_trip()

    for row in body["flights"]:
        assert ID_RE.fullmatch(row["option_id"])
        assert "quote_id" not in row and "price" not in row
    # What the supplier prices here is the cheapest whole trip with that flight, and the row says so by name.
    assert _flight(body, "TR0866")["trip_price_from"] == {"amount": "257.00", "currency": "USD"}
    assert "never give it as the price of this flight alone" in body["hint"]


def test_continuing_from_a_flight_asks_for_the_next_leg_with_that_flight_picked(monkeypatch):
    supplier = _supplier(monkeypatch)
    out = _round_trip(adults=2, direct_only=True)

    back = _continue(_flight(out, "TR0866")["option_id"])

    first, second = supplier.bodies(SEARCH)
    picked = _data("tmc_flight_round_trip_leg1.json")["flight"][0]["journeys"][0]["journeyId"]
    assert second == {**first, "journeyId": [picked]}
    assert (second["adultNum"], second["transferNumber"]) == (2, 1)
    assert (back["leg"], back["legs"], back["route"], back["depart_date"]) == (2, 2, "TYO-TPE", _days(34))
    assert back["chosen"] == [{
        "leg": 1, "route": "TPE-NRT", "depart_local": "2026-12-16 06:50", "arrive_local": "2026-12-16 10:55",
        "flights": ["TR0866"]}]


def test_the_last_leg_gives_quotes_for_the_whole_trip(monkeypatch):
    _supplier(monkeypatch)

    back = _continue(_flight(_round_trip(adults=2), "TR0866")["option_id"])

    row = _flight(back, "GK0013")
    assert "option_id" not in row and "trip_price_from" not in row
    fare = quotes._quotes[row["quote_id"]].facts
    assert quotes._quotes[row["quote_id"]].kind == quotes.FLIGHT
    assert fare.trip_kind == quotes.ROUND_TRIP
    assert [(leg.flight_no, leg.origin, leg.destination, leg.journey) for leg in fare.legs] == [
        ("TR0866", "TPE", "NRT", 1), ("GK0013", "NRT", "TPE", 2)]
    assert fare.adults == 2
    assert fare.total.amount == Decimal(row["price"]["amount"]) == Decimal("257.00")
    assert "whole round trip" in back["hint"]


def test_the_price_key_and_the_suppliers_journey_ids_never_reach_the_model(monkeypatch):
    _supplier(monkeypatch)

    out = tools_flights.search_flights(
        {"origin": "TPE", "destination": "TYO", "depart_date": _days(30), "return_date": _days(34)})
    back = tools_flights.search_flights({"continue_from": _flight(json.loads(out), "TR0866")["option_id"]})

    for result in (out, back):
        assert "priceKey" not in result and "leg2-" not in result
        assert "journeyId" not in result and "1970c00c" not in result


def test_a_trip_home_from_another_city_is_one_multi_city_search(monkeypatch):
    supplier = _supplier(monkeypatch)

    body = json.loads(tools_flights.search_flights({
        "origin": "TPE", "destination": "TYO", "depart_date": _days(30),
        "more_legs": [{"origin": "OSA", "destination": "KHH", "depart_date": _days(34)}]}))

    (sent,) = supplier.bodies(SEARCH)
    assert sent["tripType"] == 3
    assert [(leg["origin"], leg["destination"], leg["departureDate"]) for leg in sent["journeys"]] == [
        ("TPE", "TYO", _days(30)), ("OSA", "KHH", _days(34))]
    assert (body["trip"], body["leg"], body["legs"]) == ("multi_city", 1, 2)
    assert "multi-city trip" in body["hint"]


def test_each_later_leg_is_asked_with_every_flight_picked_so_far(monkeypatch):
    middle = _data("tmc_flight_round_trip_leg1.json")
    supplier = _supplier(monkeypatch, legs=[
        _data("tmc_flight_round_trip_leg1.json"), middle, _data("tmc_flight_round_trip_leg2.json")])
    first = json.loads(tools_flights.search_flights({
        "origin": "TPE", "destination": "TYO", "depart_date": _days(30),
        "more_legs": [{"origin": "TYO", "destination": "SEL", "depart_date": _days(33)},
                      {"origin": "SEL", "destination": "TPE", "depart_date": _days(36)}]}))

    second = _continue(_flight(first, "TR0866")["option_id"])
    third = _continue(_flight(second, "MM0628")["option_id"])

    ids = {"/".join(f["journeys"][0]["flightNumberList"]): f["journeys"][0]["journeyId"] for f in middle["flight"]}
    assert [body.get("journeyId") for body in supplier.bodies(SEARCH)] == [
        None, [ids["TR0866"]], [ids["TR0866"], ids["MM0628"]]]
    assert (second["leg"], third["leg"]) == (2, 3)
    assert "option_id" in second["flights"][0] and "quote_id" in third["flights"][0]
    fare = quotes._quotes[_flight(third, "GK0013")["quote_id"]].facts
    assert fare.trip_kind == quotes.MULTI_CITY
    assert [(leg.flight_no, leg.journey) for leg in fare.legs] == [("TR0866", 1), ("MM0628", 2), ("GK0013", 3)]
    assert [row["leg"] for row in third["chosen"]] == [1, 2]


def test_two_legs_that_mirror_each_other_are_a_round_trip_however_they_are_written(monkeypatch):
    supplier = _supplier(monkeypatch)

    tools_flights.search_flights({
        "origin": "TPE", "destination": "TYO", "depart_date": _days(30),
        "more_legs": [{"origin": "TYO", "destination": "TPE", "depart_date": _days(34)}]})

    assert supplier.bodies(SEARCH)[0]["tripType"] == 2


def test_a_one_way_search_is_still_one_leg_with_quotes(monkeypatch):
    supplier = _supplier(monkeypatch, legs=[_data("tmc_flight_search.json")])
    monkeypatch.setitem(COUNTRIES, "HKG", "HK")
    monkeypatch.setitem(COUNTRIES, "SIN", "SG")

    body = json.loads(tools_flights.search_flights(
        {"origin": "HKG", "destination": "SIN", "depart_date": _days(30), "max_results": 1}))

    assert supplier.bodies(SEARCH)[0]["tripType"] == 1
    assert "trip" not in body and "leg" not in body and "chosen" not in body
    assert quotes._quotes[body["flights"][0]["quote_id"]].facts.trip_kind == quotes.ONE_WAY


# ── who may continue, and with what ──────────────────────────────────────────


def test_an_option_id_belongs_to_the_caller_who_searched(monkeypatch):
    supplier = _supplier(monkeypatch)
    option_id = _flight(_round_trip(), "TR0866")["option_id"]
    monkeypatch.setattr(_common, "caller_id", lambda: "user-b")

    code, _message = _error(tools_flights.search_flights({"continue_from": option_id}))

    assert code == "quote_unknown"
    assert len(supplier.bodies(SEARCH)) == 1


def test_an_option_id_expires_like_a_quote(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(quotes.time, "monotonic", lambda: clock[0])
    _supplier(monkeypatch)
    option_id = _flight(_round_trip(), "TR0866")["option_id"]

    clock[0] += 31 * 60

    assert _error(tools_flights.search_flights({"continue_from": option_id}))[0] == "quote_unknown"


def test_a_flight_of_an_earlier_leg_cannot_be_priced_as_a_fare(monkeypatch):
    supplier = _supplier(monkeypatch)
    option_id = _flight(_round_trip(), "TR0866")["option_id"]

    code, message = _error(tools_flights.get_flight_offer({"quote_id": option_id}))

    assert code == "trip_incomplete"
    assert "continue_from" in message
    assert not supplier.bodies(FARES) and not supplier.bodies(VERIFY)


def test_a_quote_cannot_be_continued_from(monkeypatch):
    supplier, row = _trip_quote(monkeypatch)

    code, message = _error(tools_flights.search_flights({"continue_from": row["quote_id"]}))

    assert code == "invalid_arguments"
    assert "option_id" in message
    assert len(supplier.bodies(SEARCH)) == 2


def _leg(origin: str, destination: str, offset: int) -> dict:
    return {"origin": origin, "destination": destination, "depart_date": _days(offset)}


# Each case is a function of the day: the dates are worked out when the test runs, on the clock the tool reads.
@pytest.mark.parametrize(
    "overrides, message",
    [
        (lambda: {"return_date": _days(29)}, "return_date is before"),
        (lambda: {"return_date": "34 days later"}, "yyyy-mm-dd"),
        (lambda: {"more_legs": [_leg("OSA", "KHH", 40)]}, "not both"),
        (lambda: {"return_date": None, "more_legs": []}, "more_legs lists 1 to 3"),
        (lambda: {"return_date": None, "more_legs": "OSA-KHH"}, "more_legs lists 1 to 3"),
        (lambda: {"return_date": None, "more_legs": [_leg("TYO", "OSA", 31 + n) for n in range(4)]},
         "more_legs lists 1 to 3"),
        (lambda: {"return_date": None, "more_legs": [_leg("OSA", "KHH", 29)]},
         "depart_date of a later leg is before"),
        (lambda: {"return_date": None, "more_legs": [_leg("OSA", "OSA", 34)]}, "must differ"),
        (lambda: {"return_date": None, "more_legs": [_leg("Osaka", "KHH", 34)]}, "3-letter IATA"),
    ],
    ids=["back-before-out", "not-a-date", "both-kinds", "no-legs", "not-a-list", "five-legs", "leg-before-leg",
         "nowhere", "not-a-code"],
)
def test_a_trip_that_cannot_be_flown_is_refused_before_any_search(monkeypatch, overrides, message):
    supplier = _supplier(monkeypatch)
    args = {"origin": "TPE", "destination": "TYO", "depart_date": _days(30), "return_date": _days(34), **overrides()}

    code, error = _error(tools_flights.search_flights(args))

    assert code == "invalid_arguments"
    assert message in error
    assert not supplier.bodies(SEARCH)


@pytest.mark.parametrize("extra", [
    {"depart_date": "2027-01-05"}, {"origin": "TPE", "destination": "OSA"}, {"return_date": "2027-01-09"},
    {"adults": 2}, {"cabin": "business"}, {"direct_only": False},
])
def test_a_later_leg_cannot_change_the_trip_it_continues(monkeypatch, extra):
    supplier = _supplier(monkeypatch)
    option_id = _flight(_round_trip(), "TR0866")["option_id"]

    code, message = _error(tools_flights.search_flights({"continue_from": option_id, **extra}))

    # Answering with the old trip's flights would pass them off as flights of the new request.
    assert code == "invalid_arguments"
    assert all(name in message for name in extra) and "without continue_from" in message
    assert len(supplier.bodies(SEARCH)) == 1


def test_a_leg_with_no_flights_points_back_to_the_leg_before_or_a_new_search(monkeypatch):
    _supplier(monkeypatch, legs=[_data("tmc_flight_round_trip_leg1.json"), None])

    back = _continue(_flight(_round_trip(), "TR0866")["option_id"])

    assert back["flights"] == [] and back["leg"] == 2
    assert "another option_id of the leg before" in back["hint"]
    assert "new search of the whole trip, without continue_from" in back["hint"]


def test_a_flight_the_trip_cannot_go_on_from_is_not_listed_on_an_earlier_leg(monkeypatch):
    out_flights = _data("tmc_flight_round_trip_leg1.json")
    out_flights["flight"][0]["offerPriceList"][0]["currency"] = "XYZ"  # TR0866: a price with no US dollar rate
    del out_flights["segments"]["20261216-TPE-NRT-MM0628"]["departureTime"]  # MM0628: a record too thin to book
    _supplier(monkeypatch, legs=[out_flights])

    body = _round_trip(max_results=10)

    assert [row["segments"][0]["flight"] for row in body["flights"]] == ["LJ0578"]
    assert all("option_id" in row for row in body["flights"])


def test_an_expired_option_says_to_search_the_trip_again(monkeypatch):
    _supplier(monkeypatch)

    code, message = _error(tools_flights.search_flights({"continue_from": "Q0000ABCD"}))

    assert code == "quote_unknown"
    assert "from its first leg" in message


def test_a_new_search_without_a_route_is_refused(monkeypatch):
    supplier = _supplier(monkeypatch)

    assert _error(tools_flights.search_flights({"return_date": _days(34)}))[0] == "invalid_arguments"
    assert _error(tools_flights.search_flights({"continue_from": "not an id"}))[0] == "invalid_arguments"
    assert not supplier.bodies(SEARCH)


def test_flying_out_and_back_on_the_same_day_is_a_round_trip(monkeypatch):
    supplier = _supplier(monkeypatch)

    body = _round_trip(return_date=_days(30))

    assert "error" not in body
    assert [leg["departureDate"] for leg in supplier.bodies(SEARCH)[0]["journeys"]] == [_days(30), _days(30)]


# ── the travel standard over a whole trip ────────────────────────────────────


def test_the_fare_limit_of_a_trip_is_set_by_its_first_leg(monkeypatch):
    _supplier(monkeypatch)

    out = _round_trip(max_results=10)

    # 70% above the cheapest whole trip (257): the 1,534 one is left out.
    assert out["fare_limit"] == {"amount": "436.90", "currency": "USD"}
    assert (out["total_offers"], out["outside_travel_standard"]) == (3, 1)
    assert "NH5808" not in [row["segments"][0]["flight"] for row in out["flights"]]


def test_a_later_leg_keeps_the_fare_limit_of_the_first(monkeypatch):
    back_flights = _data("tmc_flight_round_trip_leg2.json")
    for flight in back_flights["flight"]:
        offer = flight["offerPriceList"][0]
        offer["totalSalePrice"] = {"GK0013": 400, "IT0217": 430, "MM0627": 440, "CI0105": 518}[
            flight["journeys"][0]["flightNumberList"][0]]
    _supplier(monkeypatch, legs=[_data("tmc_flight_round_trip_leg1.json"), back_flights])

    back = _continue(_flight(_round_trip(), "TR0866")["option_id"], max_results=10)

    # Judged against this leg's own cheapest (400) the limit would be 680 and every flight would pass.
    assert back["fare_limit"] == {"amount": "436.90", "currency": "USD"}
    assert [row["segments"][0]["flight"] for row in back["flights"]] == ["GK0013", "IT0217"]
    assert back["outside_travel_standard"] == 2
    assert quotes._quotes[back["flights"][0]["quote_id"]].facts.fare_limit == Decimal("436.90")


def _in_cabin(data: dict, cabin: str, hours: str | None = None) -> dict:
    """The fixture's flights sold in *cabin*, each flight lasting *hours* when given."""
    for flight in data["flight"]:
        for offer in flight["offerPriceList"]:
            offer["cabinClass"] = [cabin] * len(offer["cabinClass"])
    for segment in data["segments"].values():
        if hours:
            segment["flightDuration"] = hours
    return data


def test_a_premium_cabin_is_outside_the_standard_on_a_short_round_trip(monkeypatch):
    _supplier(monkeypatch, legs=[_in_cabin(_data("tmc_flight_round_trip_leg1.json"), "business")])

    body = _round_trip(cabin="business")

    assert body["flights"] == [] and body["outside_travel_standard"] == 4
    assert "on every leg" in body["hint"]


def test_a_premium_cabin_needs_every_leg_of_the_trip_to_be_long_enough(monkeypatch):
    long_way_out = _in_cabin(_data("tmc_flight_round_trip_leg1.json"), "business", hours="9h 30m")
    short_way_back = _in_cabin(_data("tmc_flight_round_trip_leg2.json"), "business")
    _supplier(monkeypatch, legs=[long_way_out, short_way_back])

    out = _round_trip(cabin="business")
    back = _continue(_flight(out, "TR0866")["option_id"])

    assert out["total_offers"] > 0
    assert back["flights"] == [] and back["outside_travel_standard"] == 4


@pytest.mark.parametrize("cabins, allowed", [
    (["economy", "business"], True),   # economy on the short way out, business on the long way back
    (["business", "economy"], False),  # business on the short way out: outside the standard, whatever follows
])
def test_each_flight_is_judged_in_its_own_cabin_by_the_flying_time_of_its_own_leg(monkeypatch, cabins, allowed):
    long_way_back = _data("tmc_flight_round_trip_leg2.json")
    for sid, segment in long_way_back["segments"].items():
        if "-TPE-NRT-" not in sid:  # every flight back, not the flight out that the answer also describes
            segment["flightDuration"] = "9h 30m"
    for flight in long_way_back["flight"]:
        flight["offerPriceList"][0]["cabinClass"] = cabins
    _supplier(monkeypatch, legs=[_data("tmc_flight_round_trip_leg1.json"), long_way_back])

    back = _continue(_flight(_round_trip(), "TR0866")["option_id"], max_results=10)

    # Of the four flights back, one is above the fare limit either way.
    assert (len(back["flights"]), back["outside_travel_standard"]) == ((3, 1) if allowed else (0, 4))


def test_each_flight_of_a_trip_is_remembered_in_its_own_cabin(monkeypatch):
    way_out = _in_cabin(_data("tmc_flight_round_trip_leg1.json"), "business", hours="9h 30m")
    way_back = _in_cabin(_data("tmc_flight_round_trip_leg2.json"), "business", hours="9h 30m")
    # The supplier names a cabin for each flight of the whole trip, in travel order: out, then back.
    way_back["flight"][0]["offerPriceList"][0]["cabinClass"] = ["business", "premium_economy"]
    _supplier(monkeypatch, legs=[way_out, way_back])

    back = _continue(_flight(_round_trip(cabin="business"), "TR0866")["option_id"])

    fare = quotes._quotes[_flight(back, "GK0013")["quote_id"]].facts
    assert [(leg.flight_no, leg.cabin) for leg in fare.legs] == [
        ("TR0866", "business"), ("GK0013", "premium_economy")]


# ── what a row says about baggage ────────────────────────────────────────────


def test_a_row_states_the_baggage_of_its_own_flights_only(monkeypatch):
    _supplier(monkeypatch)

    row = _flight(_round_trip(max_results=10), "MM0628")

    # The fare priced on this row is the cheapest whole trip, whose rules also name a flight back nobody picked.
    assert row["baggage"] == {"carry_on": "1 x 7KG"}


@pytest.mark.parametrize(
    "piece, words",
    [
        ({"number": 1, "weight": 23, "unit": "KG"}, "1 x 23KG"),
        ({"number": -1, "weight": 20, "unit": "KG"}, "20KG"),
        ({"number": 2, "weight": -1, "unit": "KG"}, "2 pieces"),
        ({"number": -1, "weight": -1, "unit": "KG"}, None),
        ({"number": 0, "weight": 0, "unit": "KG"}, None),
    ],
)
def test_a_count_or_weight_the_supplier_does_not_know_is_never_shown_as_a_number(monkeypatch, piece, words):
    out_flights = _data("tmc_flight_round_trip_leg1.json")
    for rule in out_flights["flight"][0]["offerPriceList"][0]["baggageRules"]:
        rule["checkIn"], rule["portable"] = piece, {"number": -1, "weight": -1, "unit": "KG"}
    _supplier(monkeypatch, legs=[out_flights])

    row = _flight(_round_trip(), "TR0866")

    assert row.get("baggage") == ({"checked": words} if words else None)
    assert "-1" not in json.dumps(row.get("baggage"))


# ── the live price of a whole trip ───────────────────────────────────────────


def test_a_trip_is_priced_through_the_fares_of_its_finished_itinerary(monkeypatch):
    supplier, row = _trip_quote(monkeypatch)

    body = json.loads(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    # Verify refuses the search's own key for a trip: the itinerary's fares are read with it, and the cheapest
    # of those is what gets verified.
    assert [sent["priceKey"] for sent in supplier.bodies(FARES)] == ["leg2-GK0013-1"]
    assert [sent["priceKey"] for sent in supplier.bodies(VERIFY)] == ["fare-1"]
    assert body["price"] == {"amount": "265.00", "currency": "USD"}
    assert body["searched_price"] == {"amount": "257.00", "currency": "USD"}
    assert [(journey["route"], journey["flight_numbers"]) for journey in body["journeys"]] == [
        ("TPE-TYO", ["TR0866"]), ("TYO-TPE", ["GK0013"])]
    assert "price changed" in body["hint"]
    assert quotes._quotes[row["quote_id"]].facts.total.amount == Decimal("265.00")


def test_a_fee_the_supplier_does_not_state_is_never_shown_as_a_price(monkeypatch):
    row = _trip_quote(monkeypatch)[1]

    body = json.loads(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    # The real answer in the fixture carries chargeFee -1 on every refund window.
    assert body["refund_rules"] == [
        {"when": "before departure", "fee": "not stated by the supplier"},
        {"when": "after departure", "fee": "not stated by the supplier"}]
    assert "-1" not in json.dumps([body["refund_rules"], body["change_rules"]])


def test_the_next_fare_is_verified_when_the_cheapest_is_no_longer_sold(monkeypatch):
    def verify(key):
        return STALE if key == "fare-1" else {"code": "0", "data": _data("tmc_flight_trip_verify.json")}

    supplier, row = _trip_quote(monkeypatch, verify=verify)

    body = json.loads(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    assert [sent["priceKey"] for sent in supplier.bodies(VERIFY)] == ["fare-1", "fare-2"]
    assert body["quote_id"] == row["quote_id"]


def test_flights_that_are_not_sold_together_point_to_the_other_flights_of_the_leg(monkeypatch):
    supplier, row = _trip_quote(monkeypatch, fares={})

    code, message = _error(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    assert code == "quote_unknown"
    assert "another flight of the same leg" in message
    assert not supplier.bodies(VERIFY)


def test_a_trip_whose_every_fare_fails_the_price_check_is_not_priced(monkeypatch):
    supplier, row = _trip_quote(monkeypatch, verify=lambda key: STALE)

    assert _error(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))[0] == "quote_unknown"
    assert [sent["priceKey"] for sent in supplier.bodies(VERIFY)] == ["fare-1", "fare-2"]


def test_a_trip_fare_in_a_cabin_outside_the_standard_is_never_verified(monkeypatch):
    fares = _data("tmc_flight_trip_fares.json")
    fares["flight"][0]["offerPriceList"][0]["cabinClass"] = ["business", "business"]
    supplier, row = _trip_quote(monkeypatch, fares=fares)

    tools_flights.get_flight_offer({"quote_id": row["quote_id"]})

    assert [sent["priceKey"] for sent in supplier.bodies(VERIFY)] == ["fare-2"]


def test_a_live_price_that_cannot_be_ordered_hands_back_no_quote(monkeypatch):
    verified = copy.deepcopy(_data("tmc_flight_trip_verify.json"))
    verified["offerData"]["currency"] = "XYZ"
    row = _trip_quote(monkeypatch, verify=lambda key: {"code": "0", "data": verified})[1]

    body = json.loads(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    # The quote is withdrawn, so naming it in the answer would send the model to an order that must fail.
    assert body["price"]["unconverted"] is True and "quote_id" not in body
    assert "can no longer be ordered" in body["hint"]
    assert row["quote_id"] not in quotes._quotes


def test_a_trip_that_now_costs_more_than_its_fare_limit_loses_its_quote(monkeypatch):
    verified = copy.deepcopy(_data("tmc_flight_trip_verify.json"))
    verified["offerData"]["totalSalePrice"] = 440
    row = _trip_quote(monkeypatch, verify=lambda key: {"code": "0", "data": verified})[1]

    body = json.loads(tools_flights.get_flight_offer({"quote_id": row["quote_id"]}))

    assert body["outside_travel_standard"] is True and "quote_id" not in body
    assert row["quote_id"] not in quotes._quotes
