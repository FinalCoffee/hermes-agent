"""Order tools: a demonstration order placed from the quotes a search showed, and the caller's own orders read,
cancelled or handed over as a PDF statement.

Every order here is SIMULATED. It is recorded in the bot's order database and confirmed there, with no supplier
request, so no supplier, payment or cancel endpoint is ever called. Its prices come only from quotes (see quotes.py),
in US dollars, and it is taken only from a private Telegram chat, for the person who sent the message.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from gateway.session_context import get_session_env

from . import _common, fx, orderbridge, payment, quotes, statement, statement_files
from ._common import ApiError, ToolSpec
from .orderstore import db, store

logger = logging.getLogger(__name__)
T = TypeVar("T")

PLACE_ORDER_MAX_ITEMS = 3
MAX_TICKETS_PER_ITEM = 10
MAX_TRAVELLERS = 9
ORDERS_PER_BUYER_PER_DAY = 100
ORDERS_PER_DAY = 300
# A statement is mailed to an address the buyer typed, which nobody has confirmed: these keep the bot from being
# used to fill someone's mailbox, and bound the bounces that mail to made-up addresses brings on the sending
# account. A buyer with more orders than this in a day still has each PDF in the chat.
STATEMENT_MAILS_PER_BUYER_PER_DAY = 5
STATEMENT_MAILS_PER_MAILBOX_PER_DAY = 3
STATEMENT_MAILS_PER_DAY = 100
ACTOR = "accomy-bot"  # who wrote the rows: a component of the bot, never a person
LOCALE = "zh-TW"
BOOKING_CODE_RE = re.compile(r"AC[0-9A-HJKMNP-TV-Z]{12}")
TRIP_CODE_RE = re.compile(r"TR[0-9A-HJKMNP-TV-Z]{12}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# What a statement may be mailed to: a plain mailbox on a plain host name, narrower than what a contact may
# hold. The payment page holds the same pattern (_ADDRESS_RE in deploy/accomy/payment-demo/handler.py).
_MAILBOX_RE = re.compile(
    r"(?=[^@]{1,64}@)[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_PHONE_RE = re.compile(r"\+?[0-9][0-9 ()\-]{5,30}")
_TRAVELLER_KINDS = ("ADULT", "CHILD", "INFANT")
_LOCK_KEY = "accomy.orders.place"

DEMO_NOTICE = (
    "Demonstration order: it has not been placed with the supplier, and no payment was taken. "
    "No supplier confirmation number or e-ticket exists for it.")
CANCEL_NOTICE = "Cancelled. The demonstration order had no supplier, and no payment was taken, so nothing is refunded."
CANCEL_PAID_NOTICE = (
    "Cancelled. The user had completed the demonstration payment for this order: that page charges nothing, so "
    "there is nothing to refund.")
ONLY_IN_PRIVATE_CHAT = (
    "Orders can only be placed in a private Telegram chat with this assistant. Ask the user to order there.")
UNAVAILABLE = "Ordering is temporarily unavailable. Tell the user to try again in a few minutes."
GENERIC_FAILURE = "The order could not be completed. Tell the user to try again later."
CANCELLED_EARLIER = (
    "An order for these quotes was placed and cancelled earlier today. Search again for current prices, then "
    "order with the new quote ids.")
BUYER_LIMIT = (
    f"The daily limit of {ORDERS_PER_BUYER_PER_DAY} orders for this chat is reached. "
    "Tell the user to try again tomorrow.")
DAY_LIMIT = "Orders are full for today. Tell the user to try again tomorrow."
TRIP_INCOMPLETE = (
    "That id is a flight picked for one leg of a round trip or a multi-city trip, not a fare. Call search_flights "
    "with continue_from set to it, choose the remaining legs, and order the quote_id of the last leg.")

# A refused write is answered with a fixed sentence per store code: the store's own text is never shown.
_STORE_MESSAGES = {
    "idempotency_conflict": "An order with these quotes was already placed with other details. "
                            "Ask the user to check list_trips.",
    "no_trip": "This chat has no open trip with that trip_code. Use list_trips, or leave trip_code out to start "
               "a new trip.",
    "bad_move": "That order is already cancelled or finished.",
    "supplier_order_live": "That order cannot be cancelled here.",
    "stale": "That order changed while it was being handled. Try again.",
    "no_items": "An order needs at least one item.",
    "bad_key": "This order could not be saved with those details. Search again for fresh quote ids.",
    "bad_amount": "This order could not be saved with those details. Search again for fresh quote ids.",
    "bad_time": "This order could not be saved with those details. Search again for fresh quote ids.",
    "bad_dates": "This order could not be saved with those details. Search again for fresh quote ids.",
    "no_segments": "This order could not be saved with those details. Search again for fresh quote ids.",
    "bad_rate": "This order could not be saved with those details. Search again for fresh quote ids.",
    "bad_mode": "This order could not be saved with those details. Search again for fresh quote ids.",
    "code_exhausted": UNAVAILABLE,
}


# ---- the buyer -------------------------------------------------------------------------------------

def _buyer() -> store.Buyer:
    """The sender of this private chat, as the gateway bound it. Never the shared fallback of caller_id(): a group
    or a session without a sender cannot order."""
    actor = get_session_env("HERMES_SESSION_ACTOR_ID")
    if (get_session_env("HERMES_SESSION_PLATFORM") != "telegram"
            or get_session_env("HERMES_SESSION_CHAT_TYPE") != "dm" or not actor):
        raise ApiError("not_private_chat", ONLY_IN_PRIVATE_CHAT)
    return store.Buyer("telegram", actor, get_session_env("HERMES_SESSION_CHAT_ID") or None)


# ---- argument parsing ------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pick:
    quote_id: str
    quantity: int | None


def _text(value: Any, label: str, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise ValueError(f"{label} is required and at most {limit} characters.")
    # Control and direction-override characters: PostgreSQL refuses NUL, and the rest can reorder a later display.
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in text):
        raise ValueError(f"{label} must be plain text.")
    return text


def _picks(args: dict) -> list[_Pick]:
    raw = args.get("items")
    if not isinstance(raw, list) or not 1 <= len(raw) <= PLACE_ORDER_MAX_ITEMS:
        raise ValueError(f"items lists 1 to {PLACE_ORDER_MAX_ITEMS} quotes from the search results.")
    picks = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each item is an object with a quote_id and, for event tickets, a quantity.")
        quantity = entry.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int)
                                     or not 1 <= quantity <= MAX_TICKETS_PER_ITEM):
            raise ValueError(f"quantity must be a whole number from 1 to {MAX_TICKETS_PER_ITEM}.")
        picks.append(_Pick(quotes.parse_id(entry.get("quote_id")), quantity))
    if len({pick.quote_id for pick in picks}) != len(picks):
        raise ValueError("each quote_id may appear only once.")
    return picks


def _contact(args: dict) -> store.Contact:
    raw = args.get("contact")
    if not isinstance(raw, dict):
        raise ValueError("contact is an object with the lead traveller's name and a phone or an email.")
    name = _text(raw.get("name"), "contact.name", 200)
    email = raw.get("email")
    phone = raw.get("phone")
    if email is not None and (not isinstance(email, str) or len(email) > 320 or not _EMAIL_RE.fullmatch(email)):
        raise ValueError("contact.email is not an email address.")
    if phone is not None and (not isinstance(phone, str) or not _PHONE_RE.fullmatch(phone)):
        raise ValueError("contact.phone holds digits, spaces and + - ( ) only.")
    if not email and not phone:
        raise ValueError("contact needs a phone number or an email address as well as the name.")
    return store.Contact(name=name, email=email or None, phone=phone or None)


def _travellers(args: dict) -> tuple[store.Traveller, ...]:
    raw = args.get("travellers") or []
    if not isinstance(raw, list) or len(raw) > MAX_TRAVELLERS:
        raise ValueError(f"travellers is a list of up to {MAX_TRAVELLERS} people.")
    people = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each traveller is an object with given_name and family_name.")
        kind = str(entry.get("kind") or "ADULT").upper()
        if kind not in _TRAVELLER_KINDS:
            raise ValueError(f"traveller kind must be one of: {', '.join(_TRAVELLER_KINDS)}.")
        people.append(store.Traveller(
            kind, given_name=_text(entry.get("given_name"), "given_name", 100),
            family_name=_text(entry.get("family_name"), "family_name", 100)))
    return tuple(people)


def _trip(args: dict) -> tuple[str | None, str | None]:
    """The trip to add the order to, or the name of the trip it starts."""
    code, title = args.get("trip_code"), args.get("trip_title")
    if code is not None:
        code = str(code).strip().upper()
        if not TRIP_CODE_RE.fullmatch(code):
            raise ValueError("trip_code is a code from list_trips or an earlier order, such as TR1234ABCD5678.")
    return code, None if title is None else _text(title, "trip_title", 80)


def _language(args: dict) -> str:
    """The language of the conversation, as the model names it: the payment page and the statement of an order are
    written in it. English when the model names none."""
    language = args.get("language")
    if language is None:
        return payment.FALLBACK_LANGUAGE
    if language not in payment.PAGE_LANGUAGES:
        raise ValueError(
            f"language is one of: {', '.join(payment.PAGE_LANGUAGES)}. Give {payment.FALLBACK_LANGUAGE} for any other.")
    return language


def _booking_code(args: dict) -> str:
    code = str(args.get("booking_code") or "").strip().upper()
    if not BOOKING_CODE_RE.fullmatch(code):
        raise ValueError("booking_code is the code from place_order, such as AC1234ABCD5678.")
    return code


def _bounded(args: dict, name: str, default: int, low: int, high: int) -> int:
    raw = args.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}.")
    return raw


# ---- building the order from quotes ----------------------------------------------------------------

def _conversion(price: fx.Converted) -> store.FxConversion | None:
    if price.source_currency == fx.BASE:
        return None
    return store.FxConversion(price.source_amount, price.source_currency, price.rate, fx.AS_OF, fx.PROVIDER)


def _hotel_item(facts: quotes.HotelRate, travellers: tuple[store.Traveller, ...]) -> store.NewItem:
    if not travellers:
        raise ValueError("A hotel needs the lead guest's name: give at least one traveller.")
    # A title is shown on the payment page, in whatever language its visitor reads: it holds names and no word.
    return store.NewItem(
        source="TMC", title=" / ".join(filter(None, (facts.hotel_name, facts.room_name)))[:200],
        sell_amount=facts.total.amount,
        detail=store.HotelStay(
            hotel_id=str(facts.hotel_id), hotel_name=facts.hotel_name[:200], check_in=facts.check_in,
            check_out=facts.check_out, adults=facts.adults, rooms=facts.rooms,
            room_id=None if facts.room_id is None else str(facts.room_id), room_name=facts.room_name or None,
            rate_plan_id=facts.rate_plan_id, supply_code=facts.supply_code,
            cancellation_note=facts.cancellation),
        travellers=travellers, conversion=_conversion(facts.total))


def _flight_item(facts: quotes.FlightFare, travellers: tuple[store.Traveller, ...]) -> store.NewItem:
    if len(travellers) != facts.adults:
        raise ValueError(f"A flight needs every passenger's name: give {facts.adults} travellers.")
    legs = tuple(
        store.FlightSegment(
            journey=leg.journey, carrier=leg.carrier, flight_no=leg.flight_no, origin=leg.origin,
            destination=leg.destination, depart_local=leg.depart_local, arrive_local=leg.arrive_local,
            cabin=leg.cabin)
        for leg in facts.legs)
    # One route per leg of the trip: a round trip reads "TPE → NRT, NRT → TPE". An arrow and not a word, because
    # the payment page shows the title in whatever language its visitor reads.
    by_leg = [[leg for leg in legs if leg.journey == number] for number in sorted({leg.journey for leg in legs})]
    route = ", ".join(f"{flown[0].origin} → {flown[-1].destination}" for flown in by_leg)
    flights = ", ".join(leg.flight_no for leg in legs)  # the supplier's flight number already carries the airline code
    return store.NewItem(
        source="TMC", title=f"{flights}: {route}"[:200], sell_amount=facts.total.amount, quantity=facts.adults,
        detail=store.FlightItinerary(trip_kind=facts.trip_kind, segments=legs, adults=facts.adults),
        travellers=travellers, conversion=_conversion(facts.total))


def _event_item(facts: quotes.EventListing, quantity: int) -> store.NewItem:
    if quantity not in facts.allowed:
        choices = ", ".join(str(n) for n in facts.allowed) or "none"
        raise ValueError(f"That quantity is not allowed for this listing. Allowed quantities: {choices}.")
    price = fx.to_usd(facts.unit_amount * quantity, facts.currency)  # a remembered listing always has a rate
    return store.NewItem(
        source="SEATUNIQUE", title=f"{facts.event_name} / {facts.listing_name}"[:200],
        sell_amount=price.amount, quantity=quantity,
        detail=store.EventTickets(
            event_id=facts.event_id, event_name=facts.event_name, listing_id=facts.listing_id,
            category_name=facts.category, venue_name=facts.venue, starts_local=facts.starts_local,
            tz_name=facts.tz_name),
        conversion=_conversion(price))


def _client_key(picks: list[_Pick], day: str) -> str:
    """The same quotes, quantities and calendar day give the same key, so a repeat or a double tap is a replay."""
    parts = sorted(f"{pick.quote_id}x{pick.quantity or 1}" for pick in picks)
    return "o" + hashlib.sha256(("|".join(parts) + "|" + day).encode()).hexdigest()[:32]


def _build(picks: list[_Pick], travellers: tuple[store.Traveller, ...], buyer: store.Buyer,
           contact: store.Contact, day: str, trip: tuple[str | None, str | None]) -> store.NewBooking:
    items = []
    for pick in picks:
        quote = quotes.lookup(pick.quote_id)
        if quote.kind == quotes.HOTEL:
            if pick.quantity is not None:
                raise ValueError("quantity applies to event tickets only.")
            items.append(_hotel_item(quote.facts, travellers))
        elif quote.kind == quotes.FLIGHT:
            if pick.quantity is not None:
                raise ValueError("quantity applies to event tickets only.")
            items.append(_flight_item(quote.facts, travellers))
        elif quote.kind == quotes.FLIGHT_CHOICE:
            raise ApiError("trip_incomplete", TRIP_INCOMPLETE)
        else:
            items.append(_event_item(quote.facts, pick.quantity or 1))
    return store.NewBooking(
        buyer=buyer, currency=fx.BASE, locale=LOCALE, items=tuple(items), contact=contact,
        client_key=_client_key(picks, day), mode="SIMULATED", trip_code=trip[0], trip_title=trip[1])


# ---- the store, from a handler thread --------------------------------------------------------------

def _store_call(work: Callable[[Any], Awaitable[T]]) -> T:
    """Run *work* in one transaction on a fresh connection. The connection string is read here, on the handler's
    thread, where the profile's secrets are bound; the bridge thread never reads them. Every failure is answered with
    a fixed sentence: the driver's and the database's texts name hosts and credentials."""
    dsn = _common.secret(db.DSN_ENV)
    if not dsn:
        raise ApiError("store_unavailable", UNAVAILABLE)

    async def once() -> T:
        conn = await db.connect(dsn)
        try:
            return await db.with_tx(conn, work)
        finally:
            await conn.close()

    try:
        return orderbridge.run(once)
    except ApiError:
        raise
    except store.OrderError as exc:
        raise ApiError(exc.code, _STORE_MESSAGES.get(exc.code, GENERIC_FAILURE)) from None
    except (db.StoreUnavailable, OSError):
        logger.info("accomy order store unreachable")
        raise ApiError("store_unavailable", UNAVAILABLE) from None
    except Exception:
        logger.exception("accomy order store call failed")
        raise ApiError("order_failed", GENERIC_FAILURE) from None


async def _place(conn: Any, new: store.NewBooking) -> store.Booking:
    """Create and confirm the simulated order, within the limits. The lock is per transaction, so two orders cannot
    both see the last free place. A replay returns the first order and is never refused by a limit."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", _LOCK_KEY)
    if not await store.has_client_key(conn, new.buyer, new.client_key):
        if await store.count_orders(conn, buyer=new.buyer) >= ORDERS_PER_BUYER_PER_DAY:
            raise ApiError("rate_limited", BUYER_LIMIT)
        if await store.count_orders(conn) >= ORDERS_PER_DAY:
            raise ApiError("rate_limited", DAY_LIMIT)
    booking = await store.create_booking(conn, new, actor=ACTOR)
    if booking.replayed:
        if booking.status != "CONFIRMED":
            raise ApiError("already_cancelled", CANCELLED_EARLIER)
        return booking
    return await store.confirm_simulated(conn, booking, actor=ACTOR)


async def _read(conn: Any, code: str, buyer: store.Buyer) -> store.Booking | None:
    return await store.get_booking(conn, code, buyer=buyer)


async def _cancel(conn: Any, code: str, buyer: store.Buyer) -> store.Booking:
    booking = await store.get_booking(conn, code, buyer=buyer)
    if booking is None:
        raise ApiError("not_found", NOT_FOUND)
    return await store.cancel_booking(conn, booking, actor=ACTOR)


NOT_FOUND = "No order with that booking code was found in this chat."
NO_STATEMENT = "Only a confirmed order has a statement: that order is cancelled or finished."
STATEMENT_FAILED = "The PDF could not be made. Tell the user to try again later, and read the order to them with get_order."


# ---- results ---------------------------------------------------------------------------------------

def _money(amount: Any) -> dict:
    return {"amount": f"{amount:.2f}", "currency": fx.BASE}


def _details(item: store.Item) -> dict:
    """What an order holds about one item. The payment page shows these lines too, in its visitor's language: it
    has a label for every key here and a word for every kind of trip, so no value is a word of the bot's own.
    Each is a name, a code, a date or a number. A new key or kind of trip needs its words in the page's handler
    (deploy/accomy/payment-demo/handler.py)."""
    detail = item.detail
    if item.kind == "HOTEL":
        nights = (detail["check_out_date"] - detail["check_in_date"]).days
        return _common.compact({
            "hotel": detail["hotel_name"], "room": detail.get("room_name"),
            "check_in": detail["check_in_date"].isoformat(), "check_out": detail["check_out_date"].isoformat(),
            "nights": nights, "guests": detail["adult_count"], "rooms": detail["room_count"],
        })
    if item.kind == "FLIGHT":
        return _common.compact({
            "trip": None if detail["trip_kind"] == quotes.ONE_WAY else detail["trip_kind"],
            "passengers": detail["adult_count"],
            "flights": [
                f"{s['flight_no']} {s['origin_code']} {s['depart_local']:%Y-%m-%d %H:%M} "
                f"→ {s['destination_code']} {s['arrive_local']:%Y-%m-%d %H:%M}"
                for s in detail["segments"]],
        })
    return _common.compact({
        "event": detail["event_name"], "venue": detail.get("venue_name"), "category": detail.get("category_name"),
        "starts_local": f"{detail['starts_local']:%Y-%m-%d %H:%M}" if detail.get("starts_local") else None,
        "tickets": item.quantity,
    })


def _order_view(booking: store.Booking) -> dict:
    view = {
        "booking_code": booking.code,
        "trip": {"trip_code": booking.trip.code, "title": booking.trip.title},
        "status": booking.status,
        "currency": booking.currency,
        "total": _money(booking.total_amount),
        "created_at": booking.created_at.isoformat(timespec="minutes"),
        "items": [_item_view(item) for item in booking.items],
    }
    if booking.order_mode == "SIMULATED":
        view["demo"] = True
    return view


def _item_view(item: store.Item) -> dict:
    view = {"kind": item.kind, "title": item.title, "quantity": item.quantity, "status": item.booking_status,
            "amount": _money(item.sell_amount), "details": _details(item)}
    if item.conversion is not None:
        view["approximate"] = True
        view["supplier_price"] = {
            "amount": str(item.conversion.source_amount.normalize()
                          if item.conversion.source_amount == item.conversion.source_amount.to_integral()
                          else item.conversion.source_amount.quantize(Decimal("0.01"))),
            "currency": item.conversion.source_currency}
    return view


def _with_payment(view: dict, booking: store.Booking, paid: bool, language: str) -> dict:
    """Add what the demo payment page knows: the order is paid, or here is the link to pay it, on a page that opens
    in *language*."""
    if paid:
        view["payment"] = "PAID (demonstration: nothing was charged)"
        return view
    lines = [{"kind": item["kind"], "title": item["title"], "quantity": item["quantity"],
              "amount": item["amount"]["amount"], "details": item["details"],
              **({"supplier_price": item["supplier_price"]} if "supplier_price" in item else {})}
             for item in view["items"]]
    if url := payment.link(booking, lines, language=language):
        view["payment"] = "UNPAID"
        view["payment_url"] = url
    return view


# ---- handlers --------------------------------------------------------------------------------------

@_common.tool_handler
def place_order(args: dict) -> str:
    buyer = _buyer()
    picks = _picks(args)
    contact = _contact(args)
    travellers = _travellers(args)
    day = dt.datetime.now(dt.UTC).date().isoformat()
    language = _language(args)
    new = _build(picks, travellers, buyer, contact, day, _trip(args))
    booking = _store_call(lambda conn: _place(conn, new))
    view = _with_payment(
        _order_view(booking), booking, booking.replayed and payment.state(booking) == "PAID", language)
    if booking.replayed:
        view["replayed"] = True
    elif "payment_url" in view and (mailed_to := _statement_by_mail(booking, language)):
        view["statement_email"] = mailed_to
    return _common.ok(
        **view,
        notice=DEMO_NOTICE,
        hint=(
            "Give the user the booking code once, and say this is a demonstration order that has not been placed "
            "with the supplier and that no payment was taken. Never claim a supplier confirmation number, an "
            "e-ticket or a payment. Quote the USD totals as given. When payment_url is present, give it to the "
            "user as the demo payment link, exactly as it is: the page is a demonstration that charges nothing. "
            "When statement_email is present, tell the user that the order's PDF statement will be emailed to "
            "that address once the demo payment is made. Without it no email will come: promise none, and if "
            "the user expects one, offer the PDF here in the chat with get_order_pdf. "
            "Keep the trip_code: pass it to place_order when the user orders something else for the same trip."),
    )


@_common.tool_handler
def get_order(args: dict) -> str:
    buyer = _buyer()
    code = _booking_code(args)
    booking = _store_call(lambda conn: _read(conn, code, buyer))
    if booking is None:
        raise ApiError("not_found", NOT_FOUND)
    paid = booking.status == "CONFIRMED" and payment.state(booking) == "PAID"
    return _common.ok(
        **_with_payment(_order_view(booking), booking, paid, _language(args)),
        hint="Read the order's status and items to the user. Amounts are in US dollars. payment says whether "
             "the demo payment was made; when payment_url is present, give it as the demo payment link. "
             "get_order_pdf makes a PDF of a confirmed order.")


def _day(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@_common.tool_handler
def list_trips(args: dict) -> str:
    buyer = _buyer()
    limit = _bounded(args, "limit", 5, 1, 10)
    rows = _store_call(lambda conn: store.list_trips(conn, buyer, limit=limit))
    trips = [
        _common.compact({
            "trip_code": trip["trip_code"], "title": trip["title"], "status": trip["status"],
            "starts_on": _day(trip["starts_on"]), "ends_on": _day(trip["ends_on"]),
            "total": _money(sum(b["total_amount"] for b in trip["bookings"] if b["status"] != "CANCELLED")),
            "orders": [
                {"booking_code": b["booking_code"], "kinds": b["kinds"], "summary": b["summary"],
                 "status": b["status"], "total": _money(b["total_amount"]),
                 "created_at": b["created_dt"].isoformat(timespec="minutes")}
                for b in trip["bookings"]],
        })
        for trip in rows]
    return _common.ok(
        trips=trips,
        hint="Newest first. A trip's total leaves out cancelled orders. Use get_order with a booking_code for an "
             "order's items, and pass a trip_code to place_order to add to that trip." if trips
        else "This chat has no trips yet.")


_mail_allowance = _common.RateLimiter()


def _mailbox(email: str) -> str:
    """The mailbox an address most likely delivers to, for counting: without capitals, without a "+tag" and
    without dots in the name, which the large mail providers ignore. Counting together two mailboxes that differ
    only by a dot costs one of them a mail; counting the spellings apart would let one mailbox be filled."""
    name, _, host = email.lower().partition("@")
    return f"{name.split('+', 1)[0].replace('.', '')}@{host}"


def _statement_by_mail(booking: store.Booking, language: str) -> str | None:
    """Leave the statement of a new order, written in *language*, with the payment page, which mails it to the
    contact when the order is paid. The address it will go to, or None: no address, one that cannot be mailed, an allowance used up, a page
    that sends no mail. A failure here never fails the order, which is already placed."""
    email = booking.contact.email
    if not email or not _MAILBOX_RE.fullmatch(email):
        return None
    day = 86400.0
    keys = (f"buyer:{booking.buyer.ref}", f"mailbox:{_mailbox(email)}", "all")
    limits = (STATEMENT_MAILS_PER_BUYER_PER_DAY, STATEMENT_MAILS_PER_MAILBOX_PER_DAY, STATEMENT_MAILS_PER_DAY)
    if _mail_allowance.admit(tuple((key, limit, day) for key, limit in zip(keys, limits))) is not None:
        return None
    try:
        from . import statement_pdf  # as in _statement_file

        # Drawn as paid: the page mails it only once the order is.
        page = statement.build(
            booking, payment=statement.PAID, issued=dt.datetime.now(dt.UTC).date(), language=language)
        kept = payment.keep_statement(booking, email, statement_pdf.render(page)) == "KEPT"
    except Exception:
        logger.exception("accomy statement could not be left for mailing")
        kept = False
    if not kept:
        _mail_allowance.refund(keys, 1)
    return email if kept else None


def _statement_file(booking: store.Booking, language: str) -> Path:
    """Draw the order's statement and save it. A failure is answered with a fixed sentence, like the store's: what
    a drawing library or the file system says names paths of the host."""
    page = statement.build(
        booking, payment=payment.state(booking), issued=dt.datetime.now(dt.UTC).date(), language=language)
    try:
        # Imported here, so that reportlab loads with the first statement and an install without it still has
        # every other tool.
        from . import statement_pdf

        return statement_files.save(booking.code, statement_pdf.render(page))
    except Exception:
        logger.exception("accomy statement could not be made")
        raise ApiError("statement_failed", STATEMENT_FAILED) from None


@_common.tool_handler
def get_order_pdf(args: dict) -> str:
    buyer = _buyer()
    code = _booking_code(args)
    booking = _store_call(lambda conn: _read(conn, code, buyer))
    if booking is None:
        raise ApiError("not_found", NOT_FOUND)
    if booking.status != "CONFIRMED":
        raise ApiError("not_confirmed", NO_STATEMENT)
    return _common.ok(
        booking_code=booking.code,
        send_line=f"MEDIA:{_statement_file(booking, _language(args))}",
        hint="The PDF is made and not yet sent. To send it, copy send_line into your reply on a line of its own, "
             "just above the suggestions line, unchanged from its first letter M: it is an instruction to the chat "
             "app, which takes the line out and attaches the file. Never translate it, shorten it or put a word "
             "such as File before it, and never show the path in any other way. In the reply itself, tell the "
             "user in a sentence what the file is: the statement and itinerary of a demonstration order, not a "
             "ticket, a booking confirmation or a receipt. Call this tool again each time the user wants the "
             "file: it is removed an hour after it is made.")


@_common.tool_handler
def cancel_order(args: dict) -> str:
    buyer = _buyer()
    code = _booking_code(args)
    booking = _store_call(lambda conn: _cancel(conn, code, buyer))
    was_paid = payment.void(booking) == "PAID"  # the link stops taking payment
    return _common.ok(
        **_order_view(booking),
        notice=CANCEL_PAID_NOTICE if was_paid else CANCEL_NOTICE,
        hint="Confirm the cancellation to the user. Do not offer a refund: no payment was taken.")


# ---- schemas ---------------------------------------------------------------------------------------

_LANGUAGE_SCHEMA = {
    "type": "string", "enum": list(payment.PAGE_LANGUAGES),
    "description": (
        "The language you are writing to the user in. The demo payment page and the order's PDF statement are "
        f"written in it. Give {payment.FALLBACK_LANGUAGE} for a language that is not listed."),
}


def _item_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "quote_id": {"type": "string", "description": "quote_id from a hotel rate, flight offer or event listing, e.g. Q1A2B3C4D."},
            "quantity": {"type": "integer", "description": "Event tickets only: how many, 1-10 and one of the listing's allowed quantities. Default 1."},
        },
        "required": ["quote_id"],
    }


def _place_schema() -> dict:
    return {
        "name": "place_order",
        "description": (
            "Place a DEMONSTRATION order for 1 to 3 quotes that search results showed, in US dollars. Prices come only "
            "from the quotes, never from the conversation. Before calling it, show the user a summary (each item with its "
            "dates, flights or tickets, the travellers and the total in USD) and get an explicit yes. Ask for the lead "
            "traveller's name and a phone or email first. Flights need every passenger's name; hotels need at least the "
            "lead guest. Only in a private chat. The order is not placed with the supplier and no payment is taken. "
            "Orders for one journey belong to one trip: the first order starts it, later ones pass its trip_code."),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "minItems": 1, "maxItems": PLACE_ORDER_MAX_ITEMS, "items": _item_schema()},
                "contact": {
                    "type": "object",
                    "description": "The lead traveller: name, and a phone or an email.",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["name"],
                },
                "trip_code": {"type": "string", "description": "Add this order to one of the chat's open trips (from list_trips or an earlier order). Leave out to start a new trip."},
                "trip_title": {"type": "string", "description": "A short name for the new trip, e.g. Tokyo in March. Used only when trip_code is left out."},
                "language": _LANGUAGE_SCHEMA,
                "travellers": {
                    "type": "array",
                    "description": "Everyone travelling: given and family name as on their passport. Flights need one per passenger.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "given_name": {"type": "string"},
                            "family_name": {"type": "string"},
                            "kind": {"type": "string", "enum": list(_TRAVELLER_KINDS)},
                        },
                        "required": ["given_name", "family_name"],
                    },
                },
            },
            "required": ["items", "contact", "language"],
        },
    }


def _code_schema(name: str, description: str, *, written: bool = False) -> dict:
    """A tool that takes one booking code, and the conversation's language when its answer holds something written
    for the user: the payment page or the statement."""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "booking_code": {"type": "string", "description": "The booking code, e.g. AC1234ABCD5678."},
                **({"language": _LANGUAGE_SCHEMA} if written else {})},
            "required": ["booking_code", *(["language"] if written else [])],
        },
    }


_LIST_SCHEMA = {
    "name": "list_trips",
    "description": (
        "List this chat's trips, newest first, each with the orders placed for it (flights, hotels, event tickets), "
        "their status and USD totals. Read-only."),
    "parameters": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Trips to return, 1-10 (default 5)."}},
        "required": [],
    },
}

_GET_SCHEMA = _code_schema(
    "get_order", "One of this chat's orders with its items and USD amounts. Read-only.", written=True)

_PDF_SCHEMA = _code_schema(
    "get_order_pdf",
    "Make a PDF of one of this chat's confirmed orders, to send to the user as a file: its statement and itinerary "
    "with the travellers, each flight, hotel stay or ticket, the dates and the USD total. Use it when the user "
    "asks for the order, its itinerary or its statement as a PDF, a file or a document. It is also all there is "
    "for a user who asks for a ticket, a booking confirmation or a receipt: the page says that the order is a "
    "demonstration and none of those, and so does the reply. The result holds the line that sends the file when "
    "it is copied into the reply.", written=True)

_CANCEL_SCHEMA = _code_schema(
    "cancel_order", "Cancel one of this chat's confirmed demonstration orders. No payment was taken, so nothing is refunded.")


_DSN = (db.DSN_ENV,)

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(name="place_order", schema=_place_schema(), handler=place_order, requires_env=_DSN, emoji="🧾"),
    ToolSpec(name="list_trips", schema=_LIST_SCHEMA, handler=list_trips, requires_env=_DSN, emoji="🧳"),
    ToolSpec(name="get_order", schema=_GET_SCHEMA, handler=get_order, requires_env=_DSN, emoji="🧾"),
    ToolSpec(name="get_order_pdf", schema=_PDF_SCHEMA, handler=get_order_pdf, requires_env=_DSN, emoji="📄"),
    ToolSpec(name="cancel_order", schema=_CANCEL_SCHEMA, handler=cancel_order, requires_env=_DSN, emoji="🧾"),
)
