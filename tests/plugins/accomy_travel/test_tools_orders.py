"""Order tools against the real order store (PostgreSQL 17, a throwaway schema). Quotes come from the real search
handlers; only the supplier clients are stubbed, so the prices and facts an order carries are the ones a search showed.

Skipped without the local test database (see test_orderstore.py for how to start it)."""

import asyncio
import copy
import json
import re
import secrets
import socket
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from types import SimpleNamespace

import pytest

asyncpg = pytest.importorskip("asyncpg")

from gateway.session_context import clear_session_vars, set_session_vars
from plugins.accomy_travel import _common, fx, quotes, seatunique_client, tmc_client, tools_events, tools_flights
from plugins.accomy_travel import payment, tools_hotels, tools_orders
from plugins.accomy_travel.orderstore import db, migrate, store

FIXTURES = Path(__file__).parent / "fixtures"
HOST, PORT = "127.0.0.1", 55433
DSN = f"postgresql://chatbot:accomy-test-only@{HOST}:{PORT}/chatbot"
EVENT_ID = "007bfe5b-aaf8-4ece-a105-df7e9429a6e1"
BOOKING_CODE = re.compile(r"AC[0-9A-HJKMNP-TV-Z]{12}")


def _database_is_up() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, PORT)) == 0


pytestmark = pytest.mark.skipif(not _database_is_up(), reason=f"no test PostgreSQL on {HOST}:{PORT}")


def _fixture(name: str) -> dict:
    import json as _json
    return _json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.fixture
def orders(monkeypatch):
    """A fresh order schema, the supplier clients stubbed with the fixtures, and the search tools' limits reset."""
    name = f"acc_o_{secrets.token_hex(6)}"

    async def build():
        conn = await db.connect(DSN, schema=name)
        try:
            await migrate.migrate(conn, commit=True, schema=name)
        finally:
            await conn.close()

    asyncio.run(build())
    quotes._quotes.clear()
    monkeypatch.setattr(db, "SCHEMA", name)
    monkeypatch.setattr(_common, "secret", lambda key: DSN if key == db.DSN_ENV else "")
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    monkeypatch.setattr(tmc_client, "hotel_products",
                        lambda **_kwargs: copy.deepcopy(_fixture("tmc_hotel_products.json")["data"]))
    monkeypatch.setattr(tmc_client, "hotel_detail", lambda hotel_id: _fixture("tmc_hotel_detail.json")["data"])
    monkeypatch.setattr(tmc_client, "airport_rows", lambda keyword: [{
        "cityCode": keyword, "countryCode": "HK" if keyword == "HKG" else "SG",
        "airports": [{"airportCode": keyword, "countryCode": "HK" if keyword == "HKG" else "SG", "isActive": 1}]}])
    monkeypatch.setattr(tmc_client, "flight_search", lambda body: _fixture("tmc_flight_search.json")["data"])
    event = copy.deepcopy(_fixture("su_events.json")["data"][0])
    event["id"] = EVENT_ID
    event["date_and_time"] = f"{_future(60)}T14:00:00.000+01:00"
    monkeypatch.setattr(seatunique_client, "get_event", lambda event_id: event)
    monkeypatch.setattr(seatunique_client, "list_listings", lambda event_id: _fixture("su_listings.json")["data"])
    yield name
    quotes._quotes.clear()

    async def drop():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await conn.close()

    asyncio.run(drop())


BUYER = {"actor": "1001", "chat": "1001"}
OTHER = {"actor": "2002", "chat": "2002"}


@contextmanager
def buyer(who=BUYER):
    tokens = set_session_vars(platform="telegram", chat_id=who["chat"], chat_type="dm",
                              actor_id=who["actor"], user_id=who["actor"])
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _ok(result: str) -> dict:
    body = json.loads(result)
    assert "error" not in body, body
    return body


def _error(result: str) -> tuple[str, str]:
    body = json.loads(result)
    assert "error" in body, body
    return body["code"], body["error"]


def _hotel_rate() -> dict:
    result = _ok(tools_hotels.get_hotel_rooms({"hotel_id": 10235214, "check_in": _future(30),
                                               "check_out": _future(32), "adults": 2, "rooms": 1}))
    return result["room_types"][0]["rates"][0]


def _flight_offer() -> dict:
    return _ok(tools_flights.search_flights({"origin": "HKG", "destination": "SIN",
                                              "depart_date": _future(30), "max_results": 1}))["flights"][0]


def _event_listing() -> dict:
    return _ok(tools_events.get_event_tickets({"event_id": EVENT_ID}))["listings"][0]


def _order(*quote_ids, quantity=None, travellers=None) -> str:
    items = []
    for qid in quote_ids:
        item = {"quote_id": qid}
        if quantity is not None:
            item["quantity"] = quantity
        items.append(item)
    people = [{"given_name": "Mei", "family_name": "Lin"}] if travellers is None else travellers
    return tools_orders.place_order({
        "items": items, "contact": {"name": "Mei Lin", "email": "mei@example.com"}, "travellers": people})


def _count(schema: str, sql: str, *args):
    async def go():
        conn = await db.connect(DSN, schema=schema)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


# ── Placing ───────────────────────────────────────────────────────────────────


def test_a_hotel_quote_places_a_simulated_order_in_usd_without_any_supplier_identifier(orders):
    with buyer():
        rate = _hotel_rate()
        placed = _ok(_order(rate["quote_id"]))

    assert BOOKING_CODE.fullmatch(placed["booking_code"])
    assert placed["status"] == "CONFIRMED"
    assert placed["currency"] == "USD"
    assert placed["total"] == rate["total_price"]
    assert placed["demo"] is True
    assert placed["notice"] == tools_orders.DEMO_NOTICE
    assert [item["kind"] for item in placed["items"]] == ["HOTEL"]
    assert placed["items"][0]["details"]["hotel"] == "Hotel Metropolitan Tokyo Marunouchi"
    text = json.dumps(placed)
    for secret_id in (rate["quote_id"], "5bb6fddc94044181aa3e760494f8bf80", "TS10002622", "50593980"):
        assert secret_id not in text
    assert _count(orders, "SELECT count(*) FROM o_supplier_request") == 0
    assert _count(orders, "SELECT count(*) FROM o_booking WHERE order_mode = 'SIMULATED'") == 1


def test_placing_reading_and_cancelling_an_order_sends_no_request_to_any_supplier(orders, monkeypatch):
    # With TMC credentials present, an order request would get as far as the transport, where it is recorded.
    monkeypatch.setattr(_common, "secret", lambda key: DSN if key == db.DSN_ENV else (
        "test-only" if key in (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV) else ""))
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(_common, "request_json", lambda method, url, **_kwargs: sent.append((method, url)))
    with buyer():
        items = [{"quote_id": _hotel_rate()["quote_id"]}, {"quote_id": _flight_offer()["quote_id"]},
                 {"quote_id": _event_listing()["quote_id"], "quantity": 2}]
        code = _ok(tools_orders.place_order({
            "items": items, "contact": {"name": "Mei Lin", "email": "mei@example.com"},
            "travellers": [{"given_name": "Mei", "family_name": "Lin"}]}))["booking_code"]
        _ok(tools_orders.get_order({"booking_code": code}))
        _ok(tools_orders.list_trips({}))
        _ok(tools_orders.cancel_order({"booking_code": code}))

    assert sent == []
    assert _count(orders, "SELECT count(*) FROM o_booking_item") == 3
    assert _count(orders, "SELECT count(*) FROM o_booking WHERE order_mode <> 'SIMULATED'") == 0
    assert _count(orders, "SELECT count(*) FROM o_supplier_request") == 0


def test_repeating_the_same_quotes_today_returns_the_same_order(orders):
    with buyer():
        rate = _hotel_rate()
        first = _ok(_order(rate["quote_id"]))
        again = _ok(_order(rate["quote_id"]))

    assert again["booking_code"] == first["booking_code"]
    assert again["replayed"] is True
    assert _count(orders, "SELECT count(*) FROM o_booking") == 1


def test_a_flight_order_takes_its_fare_in_usd_and_every_passenger(orders):
    with buyer():
        offer = _flight_offer()
        refused = _error(_order(offer["quote_id"], travellers=[]))
        placed = _ok(_order(offer["quote_id"]))

    assert refused[0] == "invalid_arguments"
    assert [item["kind"] for item in placed["items"]] == ["FLIGHT"]
    assert placed["total"] == offer["price"]
    assert placed["items"][0]["details"]["passengers"] == 1
    assert placed["items"][0]["details"]["flights"][0].startswith(offer["segments"][0]["flight"])


def test_an_event_order_charges_the_tickets_it_asks_for_converted_to_usd(orders):
    with buyer():
        listing = _event_listing()
        placed = _ok(_order(listing["quote_id"], quantity=2))

    expected = fx.to_usd(Decimal("238.80") * 2, "GBP")
    assert placed["total"] == {"amount": str(expected.amount), "currency": "USD"}
    assert placed["items"][0]["kind"] == "EVENT_TICKET"
    assert placed["items"][0]["quantity"] == 2
    assert placed["items"][0]["details"]["event"]
    rate_rows = _count(orders, "SELECT count(*) FROM c_fx_rate WHERE base_currency = 'GBP' AND quote_currency = 'USD'")
    assert rate_rows == 1


def test_a_quote_shown_to_one_buyer_cannot_be_ordered_by_another(orders):
    with buyer():
        rate = _hotel_rate()
    with buyer(OTHER):
        assert _error(_order(rate["quote_id"]))[0] == "quote_unknown"


def test_an_expired_quote_is_refused_and_so_is_an_order_that_asks_for_too_much(orders, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(quotes.time, "monotonic", lambda: clock[0])
    with buyer():
        rate = _hotel_rate()
        clock[0] = 1000.0 + 31 * 60
        assert _error(_order(rate["quote_id"]))[0] == "quote_unknown"


# ── Reading and cancelling ────────────────────────────────────────────────────


def test_orders_are_read_only_by_their_buyer(orders):
    with buyer():
        rate = _hotel_rate()
        code = _ok(_order(rate["quote_id"]))["booking_code"]
        mine = _ok(tools_orders.get_order({"booking_code": code}))
        listed = _ok(tools_orders.list_trips({}))
    with buyer(OTHER):
        assert _error(tools_orders.get_order({"booking_code": code}))[0] == "not_found"
        assert _ok(tools_orders.list_trips({}))["trips"] == []

    assert mine["booking_code"] == code and mine["status"] == "CONFIRMED" and mine["demo"] is True
    assert [order["booking_code"] for trip in listed["trips"] for order in trip["orders"]] == [code]
    assert mine["trip"]["trip_code"] == listed["trips"][0]["trip_code"]


def test_cancelling_an_order_cancels_it_and_it_cannot_be_cancelled_twice(orders):
    with buyer():
        rate = _hotel_rate()
        code = _ok(_order(rate["quote_id"]))["booking_code"]
        cancelled = _ok(tools_orders.cancel_order({"booking_code": code}))
        again = _error(tools_orders.cancel_order({"booking_code": code}))

    assert cancelled["status"] == "CANCELLED"
    assert cancelled["items"][0]["status"] == "CANCELLED"
    assert "refund" in cancelled["notice"]
    assert again[0] == "bad_move"


def test_another_buyers_order_cannot_be_cancelled(orders):
    with buyer():
        rate = _hotel_rate()
        code = _ok(_order(rate["quote_id"]))["booking_code"]
    with buyer(OTHER):
        assert _error(tools_orders.cancel_order({"booking_code": code}))[0] == "not_found"
    with buyer():
        assert _ok(tools_orders.get_order({"booking_code": code}))["status"] == "CONFIRMED"


# ── Limits, counted in the database ───────────────────────────────────────────


def test_the_daily_limit_per_buyer_refuses_a_new_order_but_never_a_replay(orders, monkeypatch):
    monkeypatch.setattr(tools_orders, "ORDERS_PER_BUYER_PER_DAY", 1)
    with buyer():
        rate = _hotel_rate()
        first = _ok(_order(rate["quote_id"]))
        listing = _event_listing()
        refused = _error(_order(listing["quote_id"], quantity=1))
        replay = _ok(_order(rate["quote_id"]))
    with buyer(OTHER):
        other_listing = _event_listing()
        assert "booking_code" in _ok(_order(other_listing["quote_id"], quantity=1))

    assert refused == ("rate_limited", tools_orders.BUYER_LIMIT)
    assert replay["booking_code"] == first["booking_code"] and replay["replayed"] is True


def test_the_daily_limit_for_everyone_refuses_the_next_buyer(orders, monkeypatch):
    monkeypatch.setattr(tools_orders, "ORDERS_PER_DAY", 1)
    with buyer():
        _ok(_order(_hotel_rate()["quote_id"]))
    with buyer(OTHER):
        listing = _event_listing()
        assert _error(_order(listing["quote_id"], quantity=1)) == ("rate_limited", tools_orders.DAY_LIMIT)


def test_a_second_order_joins_the_trip_the_first_one_started(orders):
    with buyer():
        first = _ok(_order(_hotel_rate()["quote_id"]))
        second = _ok(tools_orders.place_order({
            "items": [{"quote_id": _event_listing()["quote_id"], "quantity": 2}],
            "contact": {"name": "Mei Lin", "email": "mei@example.com"}, "trip_code": first["trip"]["trip_code"]}))
        trips = _ok(tools_orders.list_trips({}))["trips"]

    assert second["trip"] == first["trip"] and len(trips) == 1
    trip = trips[0]
    assert [o["booking_code"] for o in trip["orders"]] == [first["booking_code"], second["booking_code"]]
    assert [o["kinds"] for o in trip["orders"]] == [["HOTEL"], ["EVENT_TICKET"]]
    assert Decimal(trip["total"]["amount"]) == Decimal(first["total"]["amount"]) + Decimal(second["total"]["amount"])


def test_a_trip_code_from_another_chat_is_refused(orders):
    with buyer(OTHER):
        theirs = _ok(_order(_hotel_rate()["quote_id"]))["trip"]["trip_code"]
    with buyer():
        code, message = _error(tools_orders.place_order({
            "items": [{"quote_id": _hotel_rate()["quote_id"]}], "contact": {"name": "Mei Lin", "phone": "+886 900 000 000"},
            "travellers": [{"given_name": "Mei", "family_name": "Lin"}], "trip_code": theirs}))

    assert code == "no_trip" and theirs not in message


def test_an_order_carries_the_demo_payment_link_until_it_is_paid_or_cancelled(orders, monkeypatch):
    page = {"state": "UNPAID", "asked": []}

    def fake_page(method, url, **_kwargs):
        route = url.split("/")[3]
        page["asked"].append((method, route))
        if route == "v" and page["state"] == "UNPAID":
            page["state"] = "VOID"
        return 200, {"state": page["state"]}

    monkeypatch.setenv(payment.SECRET_ENV, "k" * 64)
    monkeypatch.setattr(_common, "secret", lambda key: {db.DSN_ENV: DSN, payment.SECRET_ENV: "k" * 64}.get(key, ""))
    monkeypatch.setattr(payment, "_settings", SimpleNamespace(get_config=lambda key, default=None: "https://pay.example"))
    monkeypatch.setattr(_common, "request_json", fake_page)
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"]))
        unpaid = _ok(tools_orders.get_order({"booking_code": placed["booking_code"]}))
        page["state"] = "PAID"
        paid = _ok(tools_orders.get_order({"booking_code": placed["booking_code"]}))
        cancelled = _ok(tools_orders.cancel_order({"booking_code": placed["booking_code"]}))

    assert placed["payment_url"].startswith("https://pay.example/p/v2.") and placed["payment"] == "UNPAID"
    assert unpaid["payment_url"] == placed["payment_url"]
    assert paid["payment"].startswith("PAID") and "payment_url" not in paid
    assert "payment_url" not in cancelled and cancelled["notice"] == tools_orders.CANCEL_PAID_NOTICE
    assert ("POST", "v") in page["asked"]
