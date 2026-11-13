"""Seat Unique event search and ticket availability, exercised through the tool handlers.

The supplier is replaced at the one HTTP seam (``_common.request_json``), so every test sees the exact
requests the tools would send and the exact JSON they would return to the model.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib

import pytest

from plugins.accomy_travel import _common, seatunique_client, tools_events
from plugins.accomy_travel.tools_events import get_event_tickets, search_events

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EVENT_ID = "007bfe5b-aaf8-4ece-a105-df7e9429a6e1"
BASE = seatunique_client.BASE_URL


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeSeatUnique:
    """Routes GET paths to canned responses; records every call for assertions."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def __call__(self, method, url, *, headers=None, params=None, **_kwargs):
        assert method == "GET", "the events tools are read-only"
        path = url[len(BASE):]
        self.calls.append((path, params, headers))
        route = self.routes[path]
        if callable(route):
            return route(params)
        return route

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.calls]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    monkeypatch.setattr(seatunique_client, "_cache", {})
    monkeypatch.setattr(_common, "secret", lambda name: "test-token")


def _install(monkeypatch, routes: dict) -> FakeSeatUnique:
    fake = FakeSeatUnique(routes)
    monkeypatch.setattr(_common, "request_json", fake)
    return fake


def _categories_routes() -> dict:
    return {
        "/v1/categories": (200, _load("su_categories.json")),
        "/v1/categories/6a993d77-c16c-401e-afa5-9fded6ccdd88/subcategories": (200, _load("su_subcategories.json")),
        "/v1/categories/d81800a7-06a3-4288-9e80-6736ceac3a64/subcategories": (200, {"data": []}),
    }


def _events_body() -> dict:
    return _load("su_events.json")


def _future_events(count: int = 2) -> list[dict]:
    """Fixture events moved to a date that is still in the future."""
    events = copy.deepcopy(_events_body()["data"])[:count]
    for offset, event in enumerate(events):
        local = dt.datetime(2027, 6, 17 + offset, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
        event["date_and_time"] = local.isoformat(timespec="milliseconds")
    return events


def _events_page(events: list[dict], next_page=None) -> tuple[int, dict]:
    return 200, {"data": events, "metadata": {"total": len(events), "current_page": 1, "next_page": next_page}}


def _result(raw: str) -> dict:
    return json.loads(raw)


def _tomorrow() -> str:
    return (dt.date.today() + dt.timedelta(days=1)).isoformat()


# ---- search_events --------------------------------------------------------------------------------------

def test_search_returns_upcoming_events_sorted_by_date_with_local_fields(monkeypatch):
    events = _future_events(2)
    fake = _install(monkeypatch, {"/v1/events": _events_page(events[::-1])})

    result = _result(search_events({"query": "Arsenal"}))

    assert "error" not in result
    assert [event["event_id"] for event in result["events"]] == [events[0]["id"], events[1]["id"]]
    first = result["events"][0]
    assert first["date"] == "2027-06-17"
    assert first["time"] == "14:00"
    assert first["weekday"] == "Thursday"
    assert first["venue"] == "Wembley Stadium"
    assert first["city"] == "London"
    assert first["latitude"] == pytest.approx(51.562457)
    assert first["category"] == "Sport" and first["subcategory"] == "Football"
    assert "note" in result and "notice" in result
    assert fake.calls[-1][2] == {"Authorization": "Bearer test-token"}


def test_search_sends_today_as_default_lower_bound_and_scans_with_large_pages(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    _result(search_events({}))

    params = fake.calls[0][1]
    assert params["date_and_time_from"] == dt.date.today().isoformat()
    assert params["per_page"] == seatunique_client.SCAN_PAGE_SIZE
    assert params["page"] == 1
    assert "date_and_time_to" not in params


def _yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def test_an_event_that_started_earlier_today_is_listed_because_the_filter_is_by_date(monkeypatch):
    earlier = _event_at(1, dt.datetime.combine(dt.date.today(), dt.time(0, 30)).astimezone().isoformat())
    _install(monkeypatch, {"/v1/events": _events_page([earlier])})

    result = _result(search_events({}))

    assert [event["event_id"] for event in result["events"]] == [earlier["id"]]


def test_date_from_before_yesterday_is_raised_to_yesterday(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    result = _result(search_events({"date_from": "2020-01-01"}))

    assert fake.calls[0][1]["date_and_time_from"] == _yesterday()
    assert result["date_from"] == _yesterday()


def test_yesterday_is_accepted_because_the_server_date_can_run_ahead_of_the_user(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    result = _result(search_events({"date_from": _yesterday()}))

    assert "error" not in result
    assert fake.calls[0][1]["date_and_time_from"] == _yesterday()


def test_date_range_and_name_are_sent_as_supplier_filters(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    _result(search_events({"query": "Arsenal", "date_from": "2027-06-01", "date_to": "2027-06-30"}))

    params = fake.calls[0][1]
    assert params["date_and_time_from"] == "2027-06-01"
    assert params["date_and_time_to"] == "2027-07-01T12:00:00+00:00"
    assert params["event_name"] == "Arsenal"


def _instant(value: str) -> dt.datetime:
    """A supplier bound: a bare date is read as start of day UTC, anything else as an ISO instant."""
    return dt.datetime.fromisoformat(value if "T" in value else value + "T00:00:00+00:00")


def _supplier_window(events: list[dict]):
    """A stand-in for the supplier's date filter: keeps the events whose instant lies inside the requested bounds."""

    def respond(params):
        lower = _instant(params["date_and_time_from"])
        upper = _instant(params["date_and_time_to"]) if "date_and_time_to" in params else None
        kept = [
            event for event in events
            if _instant(event["date_and_time"]) >= lower
            and (upper is None or _instant(event["date_and_time"]) <= upper)
        ]
        return _events_page(kept)

    return respond


def _event_at(n: int, when: str, country: str = "United Kingdom", city: str = "London") -> dict:
    event = copy.deepcopy(_events_body()["data"][0])
    event["id"] = f"00000000-0000-4000-8000-{n:012d}"
    event["date_and_time"] = when
    event["location"] = {**event["location"], "country": country, "city": city}
    return event


def test_date_to_includes_events_late_on_that_local_day_for_any_venue_offset(monkeypatch):
    events = [
        _event_at(1, "2027-06-29T10:00:00.000+01:00"),
        _event_at(2, "2027-06-30T14:00:00.000+01:00"),  # the day's afternoon, the case a bare date_to dropped
        _event_at(3, "2027-06-30T23:30:00.000-12:00", city="Honolulu", country="United States"),
        _event_at(4, "2027-07-01T00:30:00.000+01:00"),  # the next local day: trimmed locally
    ]
    _install(monkeypatch, {"/v1/events": _supplier_window(events)})

    result = _result(search_events({"date_from": "2027-06-01", "date_to": "2027-06-30"}))

    assert [event["event_id"] for event in result["events"]] == [
        events[0]["id"], events[1]["id"], events[2]["id"]]


def test_date_to_before_the_first_returnable_date_is_an_argument_error(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    result = _result(search_events({"date_to": "2020-01-01"}))

    assert result["code"] == "invalid_arguments"
    assert fake.calls == []


def test_malformed_date_is_rejected_before_any_call(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    result = _result(search_events({"date_from": "17/06/2027"}))

    assert result["code"] == "invalid_arguments"
    assert "2027-06-01" in result["error"]
    assert fake.calls == []


@pytest.mark.parametrize("wanted", ["UK", "U.K.", "United Kingdom", "Great Britain", "Britain", "uk"])
def test_uk_names_match_every_catalog_country_inside_the_uk(monkeypatch, wanted):
    events = [
        _event_at(1, "2027-06-17T14:00:00.000+01:00", country="United Kingdom"),
        _event_at(2, "2027-06-18T14:00:00.000+01:00", country="England", city="Manchester"),
        _event_at(3, "2027-06-19T14:00:00.000+01:00", country="Wales", city="Cardiff"),
        _event_at(4, "2027-06-20T14:00:00.000+01:00", country="Scotland", city="Glasgow"),
        _event_at(5, "2027-06-21T14:00:00.000+01:00", country="Northern Ireland", city="Belfast"),
        _event_at(6, "2027-06-22T14:00:00.000+02:00", country="Italy", city="Milan"),
    ]
    _install(monkeypatch, {"/v1/events": _events_page(events)})

    result = _result(search_events({"country": wanted}))

    assert [event["country"] for event in result["events"]] == [
        "United Kingdom", "England", "Wales", "Scotland", "Northern Ireland"]


def test_other_country_names_keep_the_substring_match(monkeypatch):
    events = [
        _event_at(1, "2027-06-17T14:00:00.000+01:00", country="England", city="Manchester"),
        _event_at(2, "2027-06-18T14:00:00.000+02:00", country="Italy", city="Milan"),
    ]
    _install(monkeypatch, {"/v1/events": _events_page(events)})

    result = _result(search_events({"country": "ital"}))

    assert [event["country"] for event in result["events"]] == ["Italy"]


def test_city_and_country_filter_client_side_case_insensitively(monkeypatch):
    events = _future_events(2)
    _install(monkeypatch, {"/v1/events": _events_page(events)})

    by_city = _result(search_events({"city": "london", "country": "united kingdom"}))
    elsewhere = _result(search_events({"city": "Manchester"}))

    assert by_city["matched"] == 2
    assert elsewhere["matched"] == 0
    assert any("No events matched" in hint for hint in elsewhere["hints"])


def test_category_and_subcategory_names_map_to_ids_and_are_sent(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([]), **_categories_routes()})

    _result(search_events({"category": "sport", "subcategory": "Football"}))

    event_params = next(params for path, params, _ in fake.calls if path == "/v1/events")
    assert event_params["category_id"] == "6a993d77-c16c-401e-afa5-9fded6ccdd88"
    assert event_params["subcategory_id"] == "64714890-5307-4d9e-8f13-5ffea3301083"


def test_unknown_category_lists_the_valid_names_and_makes_no_event_call(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([]), **_categories_routes()})

    result = _result(search_events({"category": "Theatre"}))

    assert result["code"] == "invalid_arguments"
    assert "Sport" in result["error"] and "Music" in result["error"]
    assert "/v1/events" not in fake.paths()


def test_unknown_subcategory_under_a_category_names_that_categorys_subcategories(monkeypatch):
    _install(monkeypatch, {**_categories_routes(), "/v1/events": _events_page([])})

    result = _result(search_events({"category": "Sport", "subcategory": "Snooker"}))

    assert result["code"] == "invalid_arguments"
    assert "Known subcategories" in result["error"]
    assert "Football" in result["error"]


def test_category_catalog_is_cached_between_searches(monkeypatch):
    fake = _install(monkeypatch, {"/v1/events": _events_page([]), **_categories_routes()})

    _result(search_events({"category": "Sport"}))
    _result(search_events({"category": "Music"}))

    assert fake.paths().count("/v1/categories") == 1


def test_results_are_capped_and_report_how_many_matched(monkeypatch):
    events = _future_events(2)
    _install(monkeypatch, {"/v1/events": _events_page(events)})

    result = _result(search_events({"max_results": 1}))

    assert result["matched"] == 2
    assert result["returned"] == 1
    assert len(result["events"]) == 1


@pytest.mark.parametrize("max_results", [0, 21, "3", True])
def test_bad_max_results_is_rejected(monkeypatch, max_results):
    fake = _install(monkeypatch, {"/v1/events": _events_page([])})

    result = _result(search_events({"max_results": max_results}))

    assert result["code"] == "invalid_arguments"
    assert fake.calls == []


def test_scan_follows_pages_and_dedupes_rows_that_moved_between_pages(monkeypatch):
    events = _future_events(2)
    def page(params):
        if params["page"] == 1:
            return _events_page([events[0]], next_page=2)
        return _events_page([events[0], events[1]])

    fake = _install(monkeypatch, {"/v1/events": page})

    result = _result(search_events({}))

    assert [call[1]["page"] for call in fake.calls] == [1, 2]
    assert result["matched"] == 2
    assert result["truncated"] is False


def test_scan_stops_at_the_page_budget_and_says_it_was_truncated(monkeypatch):
    events = _future_events(1)
    fake = _install(monkeypatch, {"/v1/events": lambda params: _events_page(events, next_page=params["page"] + 1)})

    result = _result(search_events({}))

    assert len(fake.calls) == tools_events.MAX_SCAN_PAGES
    assert result["truncated"] is True
    assert any("narrow" in hint.lower() for hint in result["hints"])


def test_supplier_rate_limit_becomes_a_try_again_error(monkeypatch):
    _install(monkeypatch, {"/v1/events": (429, {"status": 429, "type": "rate_limited", "message": "slow down"})})

    result = _result(search_events({}))

    assert result["code"] == "rate_limited"
    assert "try again shortly" in result["error"].lower()
    assert "slow down" not in result["error"]


@pytest.mark.parametrize("status, code", [(401, "auth"), (403, "auth"), (503, "unavailable")])
def test_supplier_failures_map_to_stable_codes_without_supplier_text(monkeypatch, status, code):
    _install(monkeypatch, {"/v1/events": (status, {"status": status, "message": "internal detail token=abc"})})

    result = _result(search_events({}))

    assert result["code"] == code
    assert "token=abc" not in result["error"]


def test_unreadable_supplier_body_is_reported_not_raised(monkeypatch):
    _install(monkeypatch, {"/v1/events": (200, ["not", "an", "envelope"])})

    result = _result(search_events({}))

    assert result["code"] == "bad_response"


# ---- get_event_tickets ----------------------------------------------------------------------------------

def _listings_body() -> dict:
    return _load("su_listings.json")


def _event_routes(listings: list[dict] | None = None) -> dict:
    event = _future_events(1)[0]
    return {
        f"/v1/events/{EVENT_ID}": (200, {"data": event}),
        f"/v1/events/{EVENT_ID}/listings": (200, {"data": listings if listings is not None else _listings_body()["data"]}),
    }


def _listing(**overrides) -> dict:
    listing = {
        "id": "11111111-1111-4111-8111-111111111111",
        "event_id": EVENT_ID,
        "name": "Test listing",
        "description": None,
        "listing_type": "ticket",
        "ticket_type": "mobile",
        "selling_option": {"name": "any", "minimum_selling_quantity": 1, "maximum_selling_quantity": 10,
                           "selling_quantity": 1, "multiplier": None},
        "available_qty": 10,
        "currency": "GBP",
        "price": 1000,
        "inclusions": [],
        "images": [],
    }
    listing.update(overrides)
    return listing


def _option(name: str, low: int, high: int, multiplier=None, selling_quantity: int = 1) -> dict:
    return {"name": name, "minimum_selling_quantity": low, "maximum_selling_quantity": high,
            "selling_quantity": selling_quantity, "multiplier": multiplier}


def test_tickets_are_sorted_by_price_with_exact_money_and_sold_out_counted(monkeypatch):
    listings = _listings_body()["data"]
    sold_out = _listing(id="22222222-2222-4222-8222-222222222222", available_qty=0, price=1)
    fake = _install(monkeypatch, {**_event_routes(listings + [sold_out])})

    result = _result(get_event_tickets({"event_id": EVENT_ID.upper()}))

    assert "error" not in result
    prices = [listing["price"] for listing in result["listings"]]
    assert prices == sorted(prices, key=lambda price: float(price["amount"]))
    assert result["listings"][0]["price"] == {"amount": "238.80", "currency": "GBP"}
    assert result["sold_out_listings"] == 1
    assert result["listings_total"] == len(listings) + 1
    assert fake.paths() == [f"/v1/events/{EVENT_ID}", f"/v1/events/{EVENT_ID}/listings"]


def test_listing_view_caps_inclusions_and_keeps_only_https_images(monkeypatch):
    many = _listing(
        inclusions=[{"name": f"Perk {i}", "position": i} for i in range(9)],
        images=[{"url": "http://insecure.example/x.jpg", "alt_text": ""}, {"url": "https://img.example/a.jpg", "alt_text": ""}])
    _install(monkeypatch, _event_routes([many]))

    listing = _result(get_event_tickets({"event_id": EVENT_ID}))["listings"][0]

    assert listing["inclusions"] == [f"Perk {i}" for i in range(tools_events.MAX_INCLUSIONS)]
    assert listing["image_url"] == "https://img.example/a.jpg"


def test_only_the_cheapest_listings_are_shown_and_the_hint_says_so(monkeypatch):
    many = [_listing(id=f"00000000-0000-4000-8000-{i:012d}", price=100 + i) for i in range(tools_events.MAX_LISTINGS + 2)]
    _install(monkeypatch, _event_routes(many))

    result = _result(get_event_tickets({"event_id": EVENT_ID}))

    assert len(result["listings"]) == tools_events.MAX_LISTINGS
    assert result["listings"][0]["price"]["amount"] == "1.00"
    assert any("cheapest" in hint for hint in result["hints"])


def test_quantity_reports_total_for_a_purchasable_any_listing(monkeypatch):
    listing = _listings_body()["data"][0]
    _install(monkeypatch, _event_routes([listing]))

    view = _result(get_event_tickets({"event_id": EVENT_ID, "quantity": 2}))["listings"][0]["for_quantity"]

    assert view == {"quantity": 2, "purchasable": True, "total": {"amount": "477.60", "currency": "GBP"}}


@pytest.mark.parametrize(
    "option, quantity, purchasable, quantities_text",
    [
        (_option("any", 1, 36), 5, True, "1 to 10"),
        (_option("range", 2, 6), 4, True, "2 to 6"),
        (_option("multiple", 2, 28, multiplier=2), 3, False, "2 to 10 in steps of 2"),
        (_option("multiple", 2, 28, multiplier=2), 4, True, "2 to 10 in steps of 2"),
        (_option("altogether", 1, 10), 9, False, "exactly 10 (all remaining tickets together)"),
        (_option("altogether", 1, 10), 10, True, "exactly 10 (all remaining tickets together)"),
        (_option("avoid_one_or_three", 1, 10), 9, False, None),
        (_option("avoid_one_or_three", 1, 10), 8, True, None),
        (_option("box", 2, 6), 4, True, "2 to 6 guests, one box per booking, price per person"),
    ],
)
def test_selling_rules_decide_whether_a_quantity_is_purchasable(monkeypatch, option, quantity, purchasable, quantities_text):
    listing = _listing(selling_option=option, available_qty=10)
    _install(monkeypatch, _event_routes([listing]))

    shown = _result(get_event_tickets({"event_id": EVENT_ID, "quantity": quantity}))["listings"][0]

    assert shown["for_quantity"]["purchasable"] is purchasable
    if quantities_text is not None:
        assert shown["quantities"] == quantities_text
    if purchasable:
        assert "total" in shown["for_quantity"]
    else:
        assert shown["for_quantity"]["reason"]


def test_avoid_one_or_three_describes_the_gaps_it_leaves(monkeypatch):
    listing = _listing(selling_option=_option("avoid_one_or_three", 1, 10), available_qty=10)
    _install(monkeypatch, _event_routes([listing]))

    text = _result(get_event_tickets({"event_id": EVENT_ID}))["listings"][0]["quantities"]

    assert text.startswith("1 to 6, 8, 10")
    assert "exactly 1 or 3" in text


def test_multiple_rule_falls_back_to_selling_quantity_when_multiplier_is_null(monkeypatch):
    listing = _listing(selling_option=_option("multiple", 2, 8, multiplier=None, selling_quantity=4), available_qty=8)
    _install(monkeypatch, _event_routes([listing]))

    view = _result(get_event_tickets({"event_id": EVENT_ID, "quantity": 6}))["listings"][0]

    assert view["quantities"] == "4 to 8 in steps of 4"
    assert view["for_quantity"]["purchasable"] is False


@pytest.mark.parametrize(
    "currency, price, expected",
    [("GBP", 1005, "10.05"), ("JPY", 1500, "1500"), ("KWD", 1234, "1.234")],
)
def test_money_uses_the_currency_minor_unit_exponent(monkeypatch, currency, price, expected):
    _install(monkeypatch, _event_routes([_listing(currency=currency, price=price)]))

    shown = _result(get_event_tickets({"event_id": EVENT_ID}))["listings"][0]

    assert shown["price"] == {"amount": expected, "currency": currency}


def test_listing_type_and_delivery_are_plain_words(monkeypatch):
    _install(monkeypatch, _event_routes([_listing(listing_type="package", ticket_type="e_ticket")]))

    shown = _result(get_event_tickets({"event_id": EVENT_ID}))["listings"][0]

    assert shown["type"] == "hospitality_package"
    assert shown["delivery"] == "e-ticket"


@pytest.mark.parametrize("bad", ["", "not-a-uuid", 12, None, "../events"])
def test_event_id_must_be_a_uuid_and_no_call_is_made(monkeypatch, bad):
    fake = _install(monkeypatch, _event_routes())

    result = _result(get_event_tickets({"event_id": bad}))

    assert result["code"] == "invalid_arguments"
    assert fake.calls == []


@pytest.mark.parametrize("quantity", [0, -1, "2", True])
def test_quantity_must_be_a_positive_whole_number(monkeypatch, quantity):
    fake = _install(monkeypatch, _event_routes())

    result = _result(get_event_tickets({"event_id": EVENT_ID, "quantity": quantity}))

    assert result["code"] == "invalid_arguments"
    assert fake.calls == []


def test_unknown_event_is_a_not_found_error(monkeypatch):
    _install(monkeypatch, {f"/v1/events/{EVENT_ID}": (404, {"status": 404, "type": "record_not_found"})})

    result = _result(get_event_tickets({"event_id": EVENT_ID}))

    assert result["code"] == "not_found"
    assert "search_events" in result["error"]


def test_result_says_purchasing_is_unavailable(monkeypatch):
    _install(monkeypatch, _event_routes([_listing()]))

    result = _result(get_event_tickets({"event_id": EVENT_ID}))

    assert "Purchasing is not available" in result["notice"]
    assert "note" in result


# ---- safety and gating ----------------------------------------------------------------------------------

@pytest.mark.parametrize("handler", [search_events, get_event_tickets])
def test_events_tools_never_touch_reservation_or_payment_endpoints(monkeypatch, handler):
    routes = {**_categories_routes(), "/v1/events": _events_page(_future_events(1)), **_event_routes([_listing()])}
    fake = _install(monkeypatch, routes)

    handler({"event_id": EVENT_ID} if handler is get_event_tickets else {"category": "Sport"})

    assert fake.calls, "the handler should have called the supplier"
    assert not any("reservation" in path or "pay" in path for path in fake.paths())


def test_both_tools_are_gated_on_the_seat_unique_token():
    names = {spec.name: spec for spec in tools_events.TOOLS}

    assert set(names) == {"search_events", "get_event_tickets"}
    assert all(spec.requires_env == ("SEATUNIQUE_API_TOKEN",) for spec in names.values())
