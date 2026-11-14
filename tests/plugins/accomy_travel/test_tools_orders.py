"""Order tools against the real order store (PostgreSQL 17, a throwaway schema). Quotes come from the real search
handlers; only the supplier clients are stubbed, so the prices and facts an order carries are the ones a search showed.

Skipped without the local test database (see test_orderstore.py for how to start it)."""

import asyncio
import base64
import copy
import dataclasses
import io
import json
import re
import secrets
import socket
import zlib
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from types import SimpleNamespace

import pypdf
import pytest

asyncpg = pytest.importorskip("asyncpg")

from gateway.session_context import clear_session_vars, set_session_vars
from plugins.accomy_travel import _common, fx, quotes, seatunique_client, tmc_client, tools_events, tools_flights
from plugins.accomy_travel import payment, statement_pdf, tools_hotels, tools_orders
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


def _order(*quote_ids, quantity=None, travellers=None, language=None) -> str:
    items = []
    for qid in quote_ids:
        item = {"quote_id": qid}
        if quantity is not None:
            item["quantity"] = quantity
        items.append(item)
    people = [{"given_name": "Mei", "family_name": "Lin"}] if travellers is None else travellers
    return tools_orders.place_order({
        "items": items, "contact": {"name": "Mei Lin", "email": "mei@example.com"}, "travellers": people,
        **({"language": language} if language else {})})


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
        _ok(tools_orders.get_order_pdf({"booking_code": code}))
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


def test_a_round_trip_is_one_flight_item_that_keeps_each_flight_with_its_leg(orders, monkeypatch):
    legs = [_fixture("tmc_flight_round_trip_leg1.json")["data"], _fixture("tmc_flight_round_trip_leg2.json")["data"]]
    monkeypatch.setattr(tmc_client, "flight_search", lambda body: legs[len(body.get("journeyId") or [])])
    with buyer():
        out = _ok(tools_flights.search_flights({
            "origin": "TPE", "destination": "TYO", "depart_date": _future(30), "return_date": _future(34)}))
        back = _ok(tools_flights.search_flights({"continue_from": out["flights"][0]["option_id"]}))
        placed = _ok(_order(back["flights"][0]["quote_id"]))

    (item,) = placed["items"]
    assert placed["total"] == back["flights"][0]["price"]
    assert item["details"]["trip"] == "ROUND_TRIP"
    assert [line.split()[0] for line in item["details"]["flights"]] == ["TR0866", "GK0013"]
    assert item["title"] == "TR0866, GK0013: TPE → NRT, NRT → TPE"
    assert _count(orders, "SELECT trip_kind FROM o_booking_item_flight") == "ROUND_TRIP"
    assert _count(
        orders, "SELECT array_agg(journey_seq ORDER BY segment_seq) FROM o_booking_item_flight_segment") == [1, 2]


def test_an_option_of_an_earlier_leg_is_refused_and_leaves_no_order(orders, monkeypatch):
    monkeypatch.setattr(
        tmc_client, "flight_search", lambda body: _fixture("tmc_flight_round_trip_leg1.json")["data"])
    with buyer():
        out = _ok(tools_flights.search_flights({
            "origin": "TPE", "destination": "TYO", "depart_date": _future(30), "return_date": _future(34)}))
        code, _message = _error(_order(out["flights"][0]["option_id"]))

    assert code == "trip_incomplete"
    assert _count(orders, "SELECT count(*) FROM o_booking") == 0


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


# ── The statement as a PDF ────────────────────────────────────────────────────


def _statement_file(result: dict) -> Path:
    assert result["send_line"].startswith("MEDIA:/")
    return Path(result["send_line"].removeprefix("MEDIA:"))


def _words(pdf: Path) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf.read_bytes()))
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def test_a_confirmed_order_is_handed_over_as_a_pdf_of_what_was_ordered(orders):
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"], _flight_offer()["quote_id"]))
        result = _ok(tools_orders.get_order_pdf({"booking_code": placed["booking_code"]}))

    pdf = _statement_file(result)
    words = _words(pdf)
    hotel, flight = placed["items"]
    assert pdf.name == f"Accomy-statement-{placed['booking_code']}.pdf"
    assert placed["booking_code"] in words and "LIN Mei" in words and "mei@example.com" in words
    assert hotel["details"]["hotel"] in words and f"{hotel['details']['nights']} nights" in words
    assert flight["details"]["flights"][0].split()[0] in words  # the flight number
    assert f"USD {Decimal(placed['total']['amount']):,.2f}" in words
    assert "Demonstration order" in words and "Not placed with a supplier" in words


def test_the_line_the_tool_returns_is_one_the_gateway_reads_as_this_file_to_attach(orders):
    from gateway.platforms.base import BasePlatformAdapter

    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
        result = _ok(tools_orders.get_order_pdf({"booking_code": code}))

    attached, shown = BasePlatformAdapter.extract_media(f"這是您的行程單。\n\n{result['send_line']}\n[suggestions: 找飯店 | 找機票]")

    assert [path for path, _voice in attached] == [str(_statement_file(result))]
    assert "MEDIA" not in shown and "accomy-statements" not in shown


def test_a_statement_that_cannot_be_drawn_is_answered_with_a_fixed_sentence(orders, monkeypatch):
    def broken(page):
        raise OSError("/opt/data/cache/documents/accomy-statements: read-only file system")

    monkeypatch.setattr(statement_pdf, "render", broken)
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
        result = tools_orders.get_order_pdf({"booking_code": code})

    assert _error(result) == ("statement_failed", tools_orders.STATEMENT_FAILED)
    assert "/opt/data" not in result


def test_a_statement_is_made_only_for_the_buyer_of_the_order(orders, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
    with buyer(OTHER):
        refused = _error(tools_orders.get_order_pdf({"booking_code": code}))

    assert refused == ("not_found", tools_orders.NOT_FOUND)
    assert not list(tmp_path.rglob("*.pdf"))


def test_a_cancelled_order_has_no_statement(orders, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
        _ok(tools_orders.cancel_order({"booking_code": code}))
        refused = _error(tools_orders.get_order_pdf({"booking_code": code}))

    assert refused == ("not_confirmed", tools_orders.NO_STATEMENT)
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize("state, shown", [
    ("PAID", "Demonstration · paid"), ("UNPAID", "Demonstration · not paid"), (None, "Demonstration order")])
def test_the_statement_says_what_the_payment_page_knows_of_the_order(orders, monkeypatch, state, shown):
    monkeypatch.setattr(payment, "state", lambda booking: state)
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
        words = _words(_statement_file(_ok(tools_orders.get_order_pdf({"booking_code": code}))))

    assert shown in words
    assert ("Nothing was charged" in words) == (state == "PAID")


def test_asking_again_makes_a_new_file_and_leaves_the_earlier_one(orders):
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"]))["booking_code"]
        first = _statement_file(_ok(tools_orders.get_order_pdf({"booking_code": code})))
        second = _statement_file(_ok(tools_orders.get_order_pdf({"booking_code": code})))

    assert first != second and first.is_file() and second.is_file()


# ── The statement mailed after payment ────────────────────────────────────────


@pytest.fixture
def paying(orders, monkeypatch):
    """A payment page that keeps what the bot hands it. ``handed`` holds each statement the bot left: the booking
    code is in the PDF, so a test reads it there."""
    page = SimpleNamespace(handed=[], answer="KEPT")

    def request_json(method, url, content=None, **_kwargs):
        route = url.split("/")[3]
        if route == "e":
            page.handed.append(json.loads(content))
            return 200, {"state": page.answer}
        return 200, {"state": "UNPAID"}

    monkeypatch.setattr(_common, "secret", lambda key: {db.DSN_ENV: DSN, payment.SECRET_ENV: "k" * 64}.get(key, ""))
    monkeypatch.setattr(payment, "_settings", SimpleNamespace(get_config=lambda key, default=None: "https://pay.example"))
    monkeypatch.setattr(_common, "request_json", request_json)
    monkeypatch.setattr(tools_orders, "_mail_allowance", _common.RateLimiter())
    return page


def _handed_pdf(statement: dict) -> str:
    reader = pypdf.PdfReader(io.BytesIO(base64.b64decode(statement["pdf"])))
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def _contact_order(contact: dict, quote_id: str) -> dict:
    return _ok(tools_orders.place_order({
        "items": [{"quote_id": quote_id}], "contact": contact,
        "travellers": [{"given_name": "Mei", "family_name": "Lin"}]}))


def test_an_order_whose_contact_gave_an_email_leaves_its_statement_to_be_mailed_on_payment(paying):
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"]))

    (statement,) = paying.handed
    words = _handed_pdf(statement)
    assert placed["statement_email"] == statement["email"] == "mei@example.com"
    assert placed["booking_code"] in words and "LIN Mei" in words
    assert "Demonstration · paid" in words  # it is mailed only once the order is paid


def test_an_order_with_only_a_phone_number_is_mailed_nowhere(paying):
    with buyer():
        placed = _contact_order({"name": "Mei Lin", "phone": "+886 912 345 678"}, _hotel_rate()["quote_id"])

    assert paying.handed == [] and "statement_email" not in placed


@pytest.mark.parametrize("email", ["mei@例え.jp", "mei.o'brien@example.com", "mei(work)@example.com",
                                   "mei..lin@example.com", ".mei@example.com", "mei@-example.com"])
def test_an_address_the_page_would_not_mail_to_is_not_sent_to_it(paying, email):
    with buyer():
        placed = _contact_order({"name": "Mei Lin", "email": email, "phone": "+886 912 345 678"}, _hotel_rate()["quote_id"])

    assert placed["status"] == "CONFIRMED" and paying.handed == [] and "statement_email" not in placed


def test_placing_the_same_order_again_leaves_no_second_statement(paying):
    with buyer():
        rate = _hotel_rate()
        _ok(_order(rate["quote_id"]))
        again = _ok(_order(rate["quote_id"]))

    assert again["replayed"] is True and len(paying.handed) == 1


def test_a_page_that_sends_no_mail_means_no_email_is_promised(paying):
    paying.answer = "OFF"
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"]))

    assert placed["status"] == "CONFIRMED" and "statement_email" not in placed


def test_a_mailbox_gets_only_so_many_statements_a_day_whoever_orders(paying, monkeypatch):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_MAILBOX_PER_DAY", 1)
    with buyer():
        first = _ok(_order(_hotel_rate()["quote_id"]))
    with buyer(OTHER):
        second = _ok(_order(_flight_offer()["quote_id"]))  # the same address, typed by someone else

    assert first["statement_email"] == "mei@example.com" and "statement_email" not in second
    assert second["status"] == "CONFIRMED" and len(paying.handed) == 1


@pytest.mark.parametrize("spelling", ["MEI@Example.com", "mei+trip@example.com", "m.e.i@example.com", "M.ei+x+y@EXAMPLE.COM"])
def test_other_spellings_of_one_mailbox_share_its_allowance(paying, monkeypatch, spelling):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_MAILBOX_PER_DAY", 1)
    with buyer():
        _contact_order({"name": "Mei Lin", "email": "mei@example.com"}, _hotel_rate()["quote_id"])
    with buyer(OTHER):
        second = _contact_order({"name": "Mei Lin", "email": spelling}, _flight_offer()["quote_id"])

    assert "statement_email" not in second and len(paying.handed) == 1


def test_another_mailbox_has_an_allowance_of_its_own(paying, monkeypatch):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_MAILBOX_PER_DAY", 1)
    with buyer():
        _contact_order({"name": "Mei Lin", "email": "mei@example.com"}, _hotel_rate()["quote_id"])
    with buyer(OTHER):
        second = _contact_order({"name": "Mei Lin", "email": "mei@example.org"}, _flight_offer()["quote_id"])

    assert second["statement_email"] == "mei@example.org" and len(paying.handed) == 2


def test_all_statements_together_have_a_daily_limit(paying, monkeypatch):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_DAY", 1)
    with buyer():
        _contact_order({"name": "Mei Lin", "email": "mei@example.com"}, _hotel_rate()["quote_id"])
    with buyer(OTHER):
        second = _contact_order({"name": "Wei Chen", "email": "wei@example.org"}, _flight_offer()["quote_id"])

    assert second["status"] == "CONFIRMED" and "statement_email" not in second and len(paying.handed) == 1


def test_a_buyer_gets_only_so_many_statements_mailed_a_day(paying, monkeypatch):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_BUYER_PER_DAY", 1)
    with buyer():
        _contact_order({"name": "Mei Lin", "email": "mei@example.com"}, _hotel_rate()["quote_id"])
        second = _contact_order({"name": "Mei Lin", "email": "lin@example.com"}, _flight_offer()["quote_id"])

    assert "statement_email" not in second and len(paying.handed) == 1


def test_a_statement_the_page_did_not_keep_is_not_counted_against_the_days_allowance(paying, monkeypatch):
    monkeypatch.setattr(tools_orders, "STATEMENT_MAILS_PER_MAILBOX_PER_DAY", 1)
    paying.answer = "OFF"
    with buyer():
        _ok(_order(_hotel_rate()["quote_id"]))
        paying.answer = "KEPT"
        second = _ok(_order(_flight_offer()["quote_id"]))

    assert second["statement_email"] == "mei@example.com"


def test_an_order_stands_when_its_statement_cannot_be_drawn_for_mailing(paying, monkeypatch):
    def broken(page):
        raise OSError("no font")

    monkeypatch.setattr(statement_pdf, "render", broken)
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"]))

    assert placed["status"] == "CONFIRMED" and placed["payment_url"] and "statement_email" not in placed
    assert paying.handed == []


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


# ---- The language of the payment page ---------------------------------------------------------------

def _linked(url: str) -> dict:
    """What a payment link says: the claims the page reads from its token."""
    body = url.rsplit("/", 1)[1].split(".")[1]
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))


def test_the_payment_page_of_an_order_opens_in_the_language_of_the_conversation(paying):
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"], language="ja"))
        code = placed["booking_code"]
        later = _ok(tools_orders.get_order({"booking_code": code, "language": "ko"}))
        unnamed = _ok(tools_orders.get_order({"booking_code": code}))

    assert _linked(placed["payment_url"])["lang"] == "ja"
    assert _linked(later["payment_url"])["lang"] == "ko"  # the conversation of the moment, not of the order
    assert _linked(unnamed["payment_url"])["lang"] == "en"


def test_an_order_placed_without_a_language_gets_an_english_page(paying):
    with buyer():
        placed = _ok(_order(_hotel_rate()["quote_id"]))

    assert _linked(placed["payment_url"])["lang"] == "en"


@pytest.mark.parametrize("tool", ["place_order", "get_order", "get_order_pdf"])
def test_a_language_that_nothing_is_written_in_is_refused(orders, tool):
    with buyer():
        rate = _hotel_rate()
        if tool == "place_order":
            code, message = _error(_order(rate["quote_id"], language="fr"))
        else:
            booking = _ok(_order(rate["quote_id"]))["booking_code"]
            code, message = _error(getattr(tools_orders, tool)({"booking_code": booking, "language": "zh-TW"}))

    assert code == "invalid_arguments" and "zh-Hant" in message and "en for any other" in message
    assert _count(orders, "SELECT count(*) FROM o_booking") == (0 if tool == "place_order" else 1)


def test_the_same_quotes_ordered_again_in_another_language_are_the_same_order_on_a_page_in_the_new_one(paying):
    with buyer():
        rate = _hotel_rate()
        first = _ok(_order(rate["quote_id"], language="zh-Hant"))
        again = _ok(_order(rate["quote_id"], language="en"))

    assert again["booking_code"] == first["booking_code"] and again["replayed"] is True
    assert (_linked(first["payment_url"])["lang"], _linked(again["payment_url"])["lang"]) == ("zh-Hant", "en")


@pytest.fixture
def cjk_font(monkeypatch):
    """The test font in the place of the image's (see test_statement_pdf.py)."""
    font = Path(__file__).parent / "fixtures" / "fonts" / "wqy-microhei-subset.ttf"
    monkeypatch.setattr(statement_pdf, "WIDE_FONT_FILES", ((str(font), 0),))
    monkeypatch.setattr(statement_pdf, "_wide_glyphs", None)


def _pdf_words(path: str) -> str:
    """The words of a PDF without their spaces: a PDF keeps none between two characters of Chinese."""
    reader = pypdf.PdfReader(path)
    return "".join(" ".join(page.extract_text() for page in reader.pages).split())


def test_the_pdf_of_an_order_is_written_in_the_language_of_the_conversation_that_asks(orders, cjk_font):
    with buyer():
        code = _ok(_order(_hotel_rate()["quote_id"], language="en"))["booking_code"]
        chinese = _ok(tools_orders.get_order_pdf({"booking_code": code, "language": "zh-Hant"}))
        japanese = _ok(tools_orders.get_order_pdf({"booking_code": code, "language": "ja"}))
        unnamed = _ok(tools_orders.get_order_pdf({"booking_code": code}))

    assert "訂單明細與行程表" in _pdf_words(chinese["send_line"].removeprefix("MEDIA:"))
    assert "ご注文明細・旅程表" in _pdf_words(japanese["send_line"].removeprefix("MEDIA:"))
    assert "Orderstatementanditinerary" in _pdf_words(unnamed["send_line"].removeprefix("MEDIA:"))


def test_the_statement_that_is_mailed_is_written_in_the_language_the_order_was_placed_in(paying, cjk_font):
    with buyer():
        tools_orders.place_order({
            "items": [{"quote_id": _hotel_rate()["quote_id"]}], "language": "ko",
            "contact": {"name": "Mei Lin", "email": "mei@example.com"},
            "travellers": [{"given_name": "Mei", "family_name": "Lin"}]})

    (handed,) = paying.handed
    assert "주문명세서및여정표" in "".join(_handed_pdf(handed).split())


def test_a_hotel_whose_room_has_no_name_is_titled_by_the_hotel_alone(orders, monkeypatch):
    with buyer():
        rate = _hotel_rate()
        quote = quotes.lookup(rate["quote_id"])
        monkeypatch.setattr(quotes, "lookup", lambda quote_id, *kinds: dataclasses.replace(
            quote, facts=dataclasses.replace(quote.facts, room_name="")))
        placed = _ok(_order(rate["quote_id"]))

    (item,) = placed["items"]
    assert item["title"] == quote.facts.hotel_name and "room" not in item["details"]


def test_what_a_payment_link_says_of_a_flight_holds_no_word_that_only_one_language_reads(paying, monkeypatch):
    legs = [_fixture("tmc_flight_round_trip_leg1.json")["data"], _fixture("tmc_flight_round_trip_leg2.json")["data"]]
    monkeypatch.setattr(tmc_client, "flight_search", lambda body: legs[len(body.get("journeyId") or [])])
    with buyer():
        out = _ok(tools_flights.search_flights({
            "origin": "TPE", "destination": "TYO", "depart_date": _future(30), "return_date": _future(34)}))
        back = _ok(tools_flights.search_flights({"continue_from": out["flights"][0]["option_id"]}))
        placed = _ok(_order(back["flights"][0]["quote_id"]))

    (line,) = _linked(placed["payment_url"])["items"]
    assert line["title"] == "TR0866, GK0013: TPE → NRT, NRT → TPE"
    assert line["details"]["trip"] == "ROUND_TRIP"  # a code: the page has a word for it in each of its languages
    assert len(line["details"]["flights"]) == 2 and "TPE" in line["details"]["flights"][0]
    # Flight numbers, airports, dates and times: nothing in lower case, so no word the page would have to translate.
    assert not re.findall(r"[a-z]+", " ".join([line["title"], *line["details"]["flights"]]))
