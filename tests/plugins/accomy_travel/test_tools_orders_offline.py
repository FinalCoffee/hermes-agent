"""Order tools that answer without the order database: who may order, which quotes they may use, what the
items may hold, and how a store that cannot be reached is reported. Every refusal here happens before a store call."""

import json
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from plugins.accomy_travel import _common, fx, orderbridge, quotes, tools_orders
from plugins.accomy_travel.orderstore import db, store

DSN_UNREACHABLE = "postgresql://chatbot:not-the-password@127.0.0.1:1/chatbot"
REAL_STORE_CALL = tools_orders._store_call


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    quotes._quotes.clear()
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    monkeypatch.setattr(_common, "secret", lambda name: DSN_UNREACHABLE if name == db.DSN_ENV else "")

    def no_store(*_args, **_kwargs):
        raise AssertionError("this refusal must not reach the order store")

    monkeypatch.setattr(tools_orders, "_store_call", no_store)
    yield
    quotes._quotes.clear()


@contextmanager
def session(actor="1001", chat_type="dm", platform="telegram", chat="1001"):
    tokens = set_session_vars(platform=platform, chat_id=chat, chat_type=chat_type, actor_id=actor, user_id=actor)
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _error(result: str) -> tuple[str, str]:
    body = json.loads(result)
    assert "error" in body, body
    return body["code"], body["error"]


def _hotel(total: str = "100", quantity_ok: bool = True) -> quotes.HotelRate:
    return quotes.HotelRate(
        hotel_id=10235214, hotel_name="Hotel Metropolitan Tokyo Marunouchi", room_id=50593980,
        room_name="Business Twin", rate_plan_id="5bb6fddc94044181aa3e760494f8bf80", rate_plan_name="Business travel",
        supply_code="TS10002622", check_in=date(2027, 3, 1), check_out=date(2027, 3, 3), adults=2, rooms=1,
        cancellation=None, total=fx.to_usd(Decimal(total), "USD"))


def _flight(adults: int = 1) -> quotes.FlightFare:
    leg = quotes.FlightLeg(
        carrier="MU", flight_no="MU0726", origin="HKG", destination="SIN",
        depart_local=__import__("datetime").datetime(2027, 3, 1, 8, 50),
        arrive_local=__import__("datetime").datetime(2027, 3, 1, 12, 30), cabin="ECONOMY")
    return quotes.FlightFare(legs=(leg,), adults=adults, total=fx.to_usd(Decimal(300), "USD"), price_key="pk")


def _event(allowed=(1, 2, 3)) -> quotes.EventListing:
    return quotes.EventListing(
        event_id="007bfe5b-aaf8-4ece-a105-df7e9429a6e1", event_name="Test Final", venue="Stadium", category="Sport",
        starts_local=__import__("datetime").datetime(2027, 6, 17, 14, 0), tz_name="UTC+01:00",
        listing_id="c1df4ade-68a9-497c-aeae-ec7cc6cf4ab7", listing_name="Hospitality", unit_amount=Decimal("238.80"),
        currency="GBP", allowed=tuple(allowed))


def _order_args(*quote_ids, travellers=None, contact=None, quantity=None) -> dict:
    items = []
    for qid in quote_ids:
        item = {"quote_id": qid}
        if quantity is not None:
            item["quantity"] = quantity
        items.append(item)
    return {
        "items": items,
        "contact": contact if contact is not None else {"name": "Mei Lin", "email": "mei@example.com"},
        "travellers": travellers if travellers is not None else [{"given_name": "Mei", "family_name": "Lin"}],
    }


def _place(args: dict) -> str:
    return tools_orders.place_order(args)


# ── Who may order ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform, chat_type, actor",
    [("telegram", "group", "1001"), ("telegram", "forum", "1001"), ("telegram", "channel", "1001"),
     ("telegram", "dm", ""), ("discord", "dm", "1001"), ("cli", "dm", "1001")])
def test_an_order_is_refused_unless_a_telegram_private_chat_names_its_sender(platform, chat_type, actor):
    with session(actor=actor, platform=platform, chat_type=chat_type):
        qid = quotes.remember(quotes.HOTEL, _hotel())
        code, message = _error(_place(_order_args(qid)))

    assert code == "not_private_chat"
    assert message == tools_orders.ONLY_IN_PRIVATE_CHAT


def test_a_flight_picked_for_one_leg_of_a_trip_cannot_be_ordered_as_a_fare():
    picked = quotes.FlightChoice(
        search={"tripType": 2}, journey_ids=("supplier-journey-1",), legs=_flight().legs, flying=(220,),
        fare_limit=None)

    with session():
        option_id = quotes.remember(quotes.FLIGHT_CHOICE, picked)
        code, message = _error(_place(_order_args(option_id)))

    assert code == "trip_incomplete"
    assert message == tools_orders.TRIP_INCOMPLETE


def test_a_command_line_run_without_any_session_cannot_order():
    qid = quotes.remember(quotes.HOTEL, _hotel())

    assert _error(_place(_order_args(qid)))[0] == "not_private_chat"


@pytest.mark.parametrize("tool, args", [
    (tools_orders.list_trips, {}),
    (tools_orders.get_order, {"booking_code": "AC0000000000000"}),
    (tools_orders.cancel_order, {"booking_code": "AC0000000000000"}),
])
def test_reading_and_cancelling_orders_is_also_private_chat_only(tool, args):
    with session(chat_type="group"):
        assert _error(tool(args))[0] == "not_private_chat"


# ── Which quotes may be used ──────────────────────────────────────────────────


def test_an_unknown_quote_is_refused_with_the_same_words_as_an_expired_one():
    with session():
        code, message = _error(_place(_order_args("Q00000000")))

    assert code == "quote_unknown"
    assert "unknown or has expired" in message


def test_an_expired_quote_is_refused(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(quotes.time, "monotonic", lambda: clock[0])
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        clock[0] = 1000.0 + 31 * 60
        assert _error(_place(_order_args(qid)))[0] == "quote_unknown"


def test_another_buyers_quote_is_refused_in_this_chat():
    with session(actor="1001"):
        qid = quotes.remember(quotes.HOTEL, _hotel())
    with session(actor="2002", chat="2002"):
        assert _error(_place(_order_args(qid)))[0] == "quote_unknown"


def test_the_same_quote_twice_is_refused():
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        assert _error(_place(_order_args(qid, qid)))[0] == "invalid_arguments"


@pytest.mark.parametrize("items", [[], [{"quote_id": "Q00000001"}] * 4])
def test_an_order_holds_one_to_three_items(items):
    with session():
        args = {"items": items, "contact": {"name": "Mei", "email": "mei@example.com"}}
        assert _error(_place(args))[0] == "invalid_arguments"


def test_quantity_is_for_event_tickets_only():
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        code, message = _error(_place(_order_args(qid, quantity=2)))

    assert code == "invalid_arguments"
    assert "event tickets" in message


def test_an_event_quantity_must_be_one_the_listing_allows_and_at_most_ten():
    with session():
        qid = quotes.remember(quotes.EVENT_TICKET, _event(allowed=(1, 2, 3)))
        assert "Allowed quantities: 1, 2, 3" in _error(_place(_order_args(qid, quantity=4)))[1]
        assert _error(_place(_order_args(qid, quantity=11)))[0] == "invalid_arguments"


# ── Who is travelling and how to reach them ───────────────────────────────────


@pytest.mark.parametrize("contact", [
    {"email": "mei@example.com"},
    {"name": "Mei Lin"},
    {"name": "Mei Lin", "email": "not-an-address"},
    {"name": "Mei Lin", "phone": "call me"},
])
def test_a_contact_needs_a_name_and_a_reachable_phone_or_email(contact):
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        assert _error(_place(_order_args(qid, contact=contact)))[0] == "invalid_arguments"


def test_a_hotel_needs_the_lead_guests_name():
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        code, message = _error(_place(_order_args(qid, travellers=[])))

    assert code == "invalid_arguments"
    assert "lead guest" in message


def test_a_flight_needs_every_passengers_name():
    with session():
        qid = quotes.remember(quotes.FLIGHT, _flight(adults=2))
        code, message = _error(_place(_order_args(qid, travellers=[{"given_name": "Mei", "family_name": "Lin"}])))

    assert code == "invalid_arguments"
    assert "give 2 travellers" in message


# ── The other tools ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 11}, {"limit": "5"}])
def test_list_trips_limit_is_bounded(args):
    with session():
        assert _error(tools_orders.list_trips(args))[0] == "invalid_arguments"


@pytest.mark.parametrize("code", ["", "AC123", "XX1234567890AB", "AC0000000000IOU"])
def test_a_booking_code_has_the_store_format(code):
    with session():
        assert _error(tools_orders.get_order({"booking_code": code}))[0] == "invalid_arguments"


# ── The store cannot be used ──────────────────────────────────────────────────


def test_without_an_order_database_the_answer_is_fixed_and_says_nothing_about_the_setup(monkeypatch):
    monkeypatch.setattr(_common, "secret", lambda name: "")
    monkeypatch.setattr(tools_orders, "_store_call", REAL_STORE_CALL)
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        code, message = _error(_place(_order_args(qid)))

    assert code == "store_unavailable"
    assert message == tools_orders.UNAVAILABLE


def test_an_unreachable_store_gives_the_fixed_answer_without_the_host(monkeypatch):
    monkeypatch.setattr(tools_orders, "_store_call", REAL_STORE_CALL)
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        result = _place(_order_args(qid))

    assert _error(result) == ("store_unavailable", tools_orders.UNAVAILABLE)
    assert "127.0.0.1" not in result and "not-the-password" not in result


def test_a_store_refusal_is_answered_with_the_fixed_sentence_for_its_code(monkeypatch):
    monkeypatch.setattr(tools_orders, "_store_call", REAL_STORE_CALL)

    def refuse(*_args, **_kwargs):
        raise store.OrderError("stale", "internal detail that must not reach the model")

    monkeypatch.setattr(orderbridge, "run", refuse)
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        result = _place(_order_args(qid))

    assert _error(result) == ("stale", tools_orders._STORE_MESSAGES["stale"])
    assert "internal detail" not in result


def test_any_other_failure_is_a_generic_answer(monkeypatch):
    monkeypatch.setattr(tools_orders, "_store_call", REAL_STORE_CALL)

    def explode(*_args, **_kwargs):
        raise RuntimeError("password=hunter2 on db.internal")

    monkeypatch.setattr(orderbridge, "run", explode)
    with session():
        qid = quotes.remember(quotes.HOTEL, _hotel())
        result = _place(_order_args(qid))

    assert _error(result) == ("order_failed", tools_orders.GENERIC_FAILURE)
    assert "hunter2" not in result and "db.internal" not in result
