"""Read and write orders. Every function takes a connection that is already inside ``db.with_tx``.

The rules come from hashtrip's order core: what was bought never changes, status moves are conditional
on the status and version the caller saw, a supplier write is logged NEW and committed before the
supplier is called, and an attempt with no clear answer blocks any further attempt until it is
reconciled. Callers pass identity (``Buyer``) from the gateway's session, never from model output.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
_CODE_ATTEMPTS = 5

BOOKING_MOVES = {
    "DRAFT": {"PENDING_PAYMENT", "CONFIRMED", "CANCELLED", "FAILED", "EXPIRED"},
    "PENDING_PAYMENT": {"CONFIRMED", "CANCELLED", "FAILED", "EXPIRED"},
    "CONFIRMED": {"PARTIALLY_CANCELLED", "CANCELLED", "COMPLETED"},
    "PARTIALLY_CANCELLED": {"CANCELLED", "COMPLETED"},
}
ITEM_MOVES = {
    "PENDING": {"CONFIRMED", "CANCELLED", "FAILED"},
    "CONFIRMED": {"CANCELLED", "COMPLETED", "NO_SHOW"},
}
FULFILMENT_MOVES = {
    "NOT_SENT": {"SENT", "NOT_APPLICABLE"},
    "SENT": {"CONFIRMED_BY_SUPPLIER", "REJECTED", "UNKNOWN"},
    "UNKNOWN": {"SENT", "CONFIRMED_BY_SUPPLIER", "REJECTED", "CANCELLED_BY_SUPPLIER"},
    "CONFIRMED_BY_SUPPLIER": {"CANCEL_SENT", "CANCELLED_BY_SUPPLIER"},
    "CANCEL_SENT": {"CANCELLED_BY_SUPPLIER", "CONFIRMED_BY_SUPPLIER", "UNKNOWN"},
}
# What a supplier request may become once the supplier has, or has not, answered.
REQUEST_MOVES = {
    "NEW": {"PLACED", "FAILED", "UNKNOWN", "CANCELLED"},
    "UNKNOWN": {"PLACED", "FAILED", "CANCELLED"},
}
# An item may be cancelled on our side only while its supplier holds no order for it.
_NO_SUPPLIER_ORDER = {"NOT_SENT", "NOT_APPLICABLE", "REJECTED", "CANCELLED_BY_SUPPLIER"}
_FINAL_BOOKING = {"CANCELLED", "COMPLETED", "FAILED", "EXPIRED"}
_CLIENT_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_RESULT_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
MODES = ("SUPPLIER", "SIMULATED")


class OrderError(Exception):
    """The store refused a write. ``code`` is a stable machine label."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Buyer:
    """Who is ordering: the channel and the account id the gateway bound to the turn."""

    channel: str
    ref: str
    chat_ref: str | None = None


@dataclass(frozen=True)
class Contact:
    name: str | None = None
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class Traveller:
    kind: str
    given_name: str | None = None
    family_name: str | None = None
    date_of_birth: date | None = None
    nationality_code: str | None = None
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class HotelStay:
    hotel_id: str
    hotel_name: str
    check_in: date
    check_out: date
    adults: int
    children: int = 0
    rooms: int = 1
    room_id: str | None = None
    room_name: str | None = None
    rate_plan_id: str | None = None
    supply_code: str | None = None
    cancellation_note: str | None = None


@dataclass(frozen=True)
class FlightSegment:
    journey: int
    carrier: str
    flight_no: str
    origin: str
    destination: str
    depart_local: datetime
    arrive_local: datetime
    cabin: str | None = None


@dataclass(frozen=True)
class FlightItinerary:
    trip_kind: str
    segments: tuple[FlightSegment, ...]
    adults: int
    children: int = 0
    infants: int = 0


@dataclass(frozen=True)
class EventTickets:
    event_id: str
    event_name: str
    listing_id: str
    category_name: str | None = None
    venue_name: str | None = None
    starts_local: datetime | None = None
    tz_name: str | None = None


_KIND_OF = {HotelStay: "HOTEL", FlightItinerary: "FLIGHT", EventTickets: "EVENT_TICKET"}


@dataclass(frozen=True)
class FxConversion:
    """How an item's price in the supplier's currency became the booking's: the rate, and when and by whom."""

    source_amount: Decimal
    source_currency: str
    rate: Decimal
    as_of: datetime
    provider: str


@dataclass(frozen=True)
class NewItem:
    source: str
    title: str
    sell_amount: Decimal
    detail: HotelStay | FlightItinerary | EventTickets
    quantity: int = 1
    travellers: tuple[Traveller, ...] = ()
    conversion: FxConversion | None = None


@dataclass(frozen=True)
class NewBooking:
    buyer: Buyer
    currency: str
    locale: str
    items: tuple[NewItem, ...]
    contact: Contact = Contact()
    client_key: str | None = None
    mode: str = "SUPPLIER"
    # The customer's existing trip to add this order to; without it the order starts a new trip.
    trip_code: str | None = None
    trip_title: str | None = None


@dataclass(frozen=True)
class Item:
    id: int
    seq: int
    kind: str
    source: str
    title: str
    quantity: int
    sell_amount: Decimal
    booking_status: str
    fulfilment_status: str
    supplier_order_no: str | None
    supplier_ref: str | None
    supplier_confirm_no: str | None
    pay_by_at: datetime | None
    version: int
    detail: dict[str, Any]
    travellers: tuple[dict[str, Any], ...]
    conversion: FxConversion | None = None


@dataclass(frozen=True)
class Trip:
    """The journey an order belongs to: the master record of one or more bookings."""

    code: str
    title: str
    starts_on: date | None
    ends_on: date | None
    status: str


@dataclass(frozen=True)
class Booking:
    id: int
    code: str
    buyer: Buyer
    contact: Contact
    locale: str
    currency: str
    status: str
    total_amount: Decimal
    version: int
    created_at: datetime
    order_mode: str = "SUPPLIER"
    items: tuple[Item, ...] = field(default=())
    replayed: bool = False
    trip: Trip | None = None


@dataclass(frozen=True)
class SupplierRequest:
    id: int
    item_id: int
    kind: str
    attempt_no: int
    client_request_key: str
    state: str
    version: int


def _money(amount: Decimal) -> Decimal:
    """Refuse an amount the column would silently round or cannot hold: numeric(18,2)."""
    if (not isinstance(amount, Decimal) or not amount.is_finite() or not 0 <= amount < Decimal("1e16")
            or amount != amount.quantize(Decimal("0.01"))):
        raise OrderError("bad_amount", "An amount must be a non-negative Decimal with at most two decimals.")
    return amount


def _wall_clock(*times: datetime | None) -> None:
    """Airport and venue times are wall-clock values: a zone on one would be shifted, or refused, on write."""
    if any(t is not None and t.tzinfo is not None for t in times):
        raise OrderError("bad_time", "A local time must not carry a time zone.")


def _check_conversion(conversion: FxConversion, currency: str) -> None:
    """The item's currency is the rate's quote, and the supplier's amount and the rate have no more decimals than
    their columns keep (four and ten): a silently rounded rate would not equal itself on the next order."""
    if (conversion.source_currency == currency or not 0 < conversion.rate < Decimal("1e10")
            or conversion.rate.quantize(Decimal("0.0000000001")) != conversion.rate
            or conversion.source_amount.quantize(Decimal("0.0001")) != conversion.source_amount
            or conversion.as_of.tzinfo is None):
        raise OrderError("bad_rate", "A converted price needs a rate, a foreign currency and an instant with a zone.")


def _check_item(item: NewItem, currency: str) -> None:
    detail = item.detail
    if item.conversion is not None:
        _check_conversion(item.conversion, currency)
    if isinstance(detail, HotelStay) and detail.check_out <= detail.check_in:
        raise OrderError("bad_dates", "Check-out must be after check-in.")
    if isinstance(detail, FlightItinerary):
        if not detail.segments:
            raise OrderError("no_segments", "A flight needs at least one segment.")
        _wall_clock(*(t for s in detail.segments for t in (s.depart_local, s.arrive_local)))
    if isinstance(detail, EventTickets):
        _wall_clock(detail.starts_local)


def _fingerprint(new: NewBooking) -> str:
    """SHA-256 of everything the request asks for, so a reused key cannot stand for another order."""
    body = {"currency": new.currency, "locale": new.locale, "mode": new.mode, "contact": dataclasses.asdict(new.contact),
            "items": [{**dataclasses.asdict(item), "kind": _KIND_OF[type(item.detail)]} for item in new.items]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=_plain).encode()).hexdigest()


def _plain(value: Any) -> str:
    # Exact text: a rate of 1.3243 must not hash like 1.32.
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def request_key(buyer: Buyer, client_key: str) -> str:
    """The stored idempotency key: namespaced by the buyer, so two buyers may use the same client key."""
    return f"{buyer.channel}:{buyer.ref}:{client_key}"


async def has_client_key(conn: Any, buyer: Buyer, client_key: str) -> bool:
    """Whether this buyer already placed a request under *client_key* (a replay would return that order)."""
    return bool(await conn.fetchval("SELECT 1 FROM o_booking WHERE idempotency_key = $1", request_key(buyer, client_key)))


async def count_orders(conn: Any, *, since_hours: int = 24, buyer: Buyer | None = None) -> int:
    """Simulated orders placed in the last *since_hours*, by one buyer or, without *buyer*, by everyone.

    Counted in the database, so a restart does not forget them. The caller holds a transaction lock while
    it counts and places, so two orders cannot both see the last free place.
    """
    if buyer is None:
        return await conn.fetchval(
            "SELECT count(*) FROM o_booking WHERE order_mode = 'SIMULATED' AND created_dt > now() - make_interval(hours => $1)",
            since_hours)
    return await conn.fetchval(
        """
        SELECT count(*) FROM o_booking WHERE order_mode = 'SIMULATED' AND buyer_channel = $1 AND buyer_ref = $2
          AND created_dt > now() - make_interval(hours => $3)
        """,
        buyer.channel, buyer.ref, since_hours)


def new_booking_code() -> str:
    return "AC" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))


def new_trip_code() -> str:
    return "TR" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))


class _LostRace(Exception):
    """Another transaction committed the same request key first; unwinds the savepoint."""


async def create_booking(conn: Any, new: NewBooking, *, actor: str) -> Booking:
    """Write a DRAFT booking with all its items. With ``client_key``, a repeat returns the first result.

    The same key for a request that differs in anything (an item, an amount, a traveller) is refused
    (``idempotency_conflict``): the key named one order and this is another. *actor* names the
    component that writes, never a person.
    """
    if not new.items:
        raise OrderError("no_items", "A booking needs at least one item.")
    if new.mode not in MODES:
        raise OrderError("bad_mode", "An order is SUPPLIER or SIMULATED.")
    for item in new.items:
        _check_item(item, new.currency)
    total = sum((_money(item.sell_amount) for item in new.items), Decimal("0.00"))
    key = fingerprint = None
    if new.client_key is not None:
        if not _CLIENT_KEY.fullmatch(new.client_key):
            raise OrderError("bad_key", "A request key is 1 to 64 letters, digits, dots, dashes or underscores.")
        key = request_key(new.buyer, new.client_key)
        fingerprint = _fingerprint(new)
        if await conn.fetchval("SELECT 1 FROM o_booking WHERE idempotency_key = $1", key):
            return await _replay(conn, key, fingerprint, new.buyer)
    try:
        async with conn.transaction():  # a savepoint: a lost race leaves no customer change and no empty trip
            customer_id = await _ensure_customer(conn, new, actor)
            trip_id = await _resolve_trip(conn, new, customer_id, actor)
            booking_id = await _insert_booking(conn, new, total, key, fingerprint, customer_id, trip_id, actor)
            if booking_id is None:  # the same key was committed between the read above and this insert
                raise _LostRace
    except _LostRace:
        return await _replay(conn, key, fingerprint, new.buyer)
    for seq, item in enumerate(new.items, start=1):
        rate_id = None
        if item.conversion is not None:
            rate_id = await _fx_rate_id(conn, new.currency, item.conversion)
        await _insert_item(conn, booking_id, seq, new.currency, item, rate_id, actor)
    await _log(conn, booking_id, None, "CREATED", None, "DRAFT", actor)
    code = await conn.fetchval("SELECT booking_code FROM o_booking WHERE id = $1", booking_id)
    return await get_booking(conn, code, buyer=new.buyer)


async def _fx_rate_id(conn: Any, currency: str, conversion: FxConversion) -> int:
    """The rate row for this pair, provider and instant, written once. The same instant with another rate is refused:
    a corrected rate is a new instant."""
    row = await conn.fetchrow(
        """
        INSERT INTO c_fx_rate (base_currency, quote_currency, rate, as_of, provider_code)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT ON CONSTRAINT c_fx_rate_uk_pair_asof DO NOTHING
        RETURNING id
        """,
        conversion.source_currency, currency, conversion.rate, conversion.as_of, conversion.provider)
    if row is not None:
        return row["id"]
    existing = await conn.fetchrow(
        """
        SELECT id, rate FROM c_fx_rate
        WHERE base_currency = $1 AND quote_currency = $2 AND provider_code = $3 AND as_of = $4
        """,
        conversion.source_currency, currency, conversion.provider, conversion.as_of)
    if existing is None or existing["rate"] != conversion.rate:
        raise OrderError("bad_rate", "The rate for this instant is already recorded with another value.")
    return existing["id"]


async def _replay(conn: Any, key: str, fingerprint: str, buyer: Buyer) -> Booking:
    code = await conn.fetchval(
        "SELECT booking_code FROM o_booking WHERE idempotency_key = $1 AND request_sha256 = $2", key, fingerprint)
    booking = await get_booking(conn, code, buyer=buyer) if code else None
    if booking is None:
        raise OrderError("idempotency_conflict", "This request key was already used for a different order.")
    return dataclasses.replace(booking, replayed=True)


async def _ensure_customer(conn: Any, new: NewBooking, actor: str) -> int:
    """The customer behind this order: found by Telegram account, else by email, else created.

    The newest contact details win, so the customer master follows what the person last told the bot.
    """
    buyer, contact = new.buyer, new.contact
    if buyer.channel == "telegram":
        return await conn.fetchval(
            """
            INSERT INTO o_customer (telegram_user_id, email, display_name, phone, locale_code, created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $6)
            ON CONFLICT (telegram_user_id) WHERE telegram_user_id IS NOT NULL DO UPDATE SET
                email = coalesce(excluded.email, o_customer.email),
                display_name = coalesce(excluded.display_name, o_customer.display_name),
                phone = coalesce(excluded.phone, o_customer.phone),
                locale_code = excluded.locale_code, version = o_customer.version + 1, updated_by = excluded.updated_by
            RETURNING id
            """,
            buyer.ref, contact.email, contact.name, contact.phone, new.locale, actor)
    if not contact.email:
        raise OrderError("no_customer", "An order outside Telegram needs a contact email to find its customer by.")
    return await conn.fetchval(
        """
        INSERT INTO o_customer (email, display_name, phone, locale_code, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $5)
        ON CONFLICT (lower(email)) WHERE telegram_user_id IS NULL AND email IS NOT NULL DO UPDATE SET
            display_name = coalesce(excluded.display_name, o_customer.display_name),
            phone = coalesce(excluded.phone, o_customer.phone),
            locale_code = excluded.locale_code, version = o_customer.version + 1, updated_by = excluded.updated_by
        RETURNING id
        """,
        contact.email, contact.name, contact.phone, new.locale, actor)


def _span(items: tuple[NewItem, ...]) -> tuple[date | None, date | None]:
    """The first and last calendar day the items cover."""
    days: list[date] = []
    for item in items:
        detail = item.detail
        if isinstance(detail, HotelStay):
            days += [detail.check_in, detail.check_out]
        elif isinstance(detail, FlightItinerary):
            days += [detail.segments[0].depart_local.date(), detail.segments[-1].arrive_local.date()]
        elif detail.starts_local is not None:
            days.append(detail.starts_local.date())
    return (min(days), max(days)) if days else (None, None)


async def _resolve_trip(conn: Any, new: NewBooking, customer_id: int, actor: str) -> int:
    """The trip this order is a detail of: the customer's named ACTIVE trip, widened to cover the new dates,
    or a new trip."""
    import asyncpg

    first, last = _span(new.items)
    if new.trip_code is not None:
        trip_id = await conn.fetchval(
            """
            UPDATE o_trip SET starts_on = least(starts_on, $3), ends_on = greatest(ends_on, $4),
                version = version + 1, updated_by = $5
            WHERE trip_code = $1 AND customer_id = $2 AND status = 'ACTIVE' RETURNING id
            """,
            new.trip_code, customer_id, first, last, actor)
        if trip_id is None:
            raise OrderError("no_trip", "This customer has no active trip with that code.")
        return trip_id
    title = " ".join((new.trip_title or new.items[0].title).split())[:200]
    for _ in range(_CODE_ATTEMPTS):
        try:
            async with conn.transaction():  # a savepoint: a code collision must not end the caller's transaction
                return await conn.fetchval(
                    """
                    INSERT INTO o_trip (trip_code, customer_id, title, starts_on, ends_on, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id
                    """,
                    new_trip_code(), customer_id, title, first, last, actor)
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "o_trip_uk_code":
                raise
    raise OrderError("code_exhausted", "Could not allocate a trip code.")


async def _insert_booking(
    conn: Any, new: NewBooking, total: Decimal, key: str | None, fingerprint: str | None, customer_id: int,
    trip_id: int, actor: str,
) -> int | None:
    import asyncpg

    for _ in range(_CODE_ATTEMPTS):
        try:
            async with conn.transaction():  # a savepoint: a code collision must not end the caller's transaction
                return await conn.fetchval(
                    """
                    INSERT INTO o_booking (booking_code, buyer_channel, buyer_ref, chat_ref, contact_name,
                        contact_email, contact_phone, locale_code, currency, total_amount, idempotency_key,
                        request_sha256, order_mode, customer_id, trip_id, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $16)
                    ON CONFLICT ON CONSTRAINT o_booking_uk_idem DO NOTHING
                    RETURNING id
                    """,
                    new_booking_code(), new.buyer.channel, new.buyer.ref, new.buyer.chat_ref, new.contact.name,
                    new.contact.email, new.contact.phone, new.locale, new.currency, total, key, fingerprint,
                    new.mode, customer_id, trip_id, actor)
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "o_booking_uk_code":
                raise
    raise OrderError("code_exhausted", "Could not allocate a booking code.")


def _source_columns(conversion: FxConversion | None) -> tuple[str | None, Decimal | None]:
    if conversion is None:
        return None, None
    return conversion.source_currency, conversion.source_amount


async def _insert_item(
    conn: Any, booking_id: int, seq: int, currency: str, item: NewItem, rate_id: int | None, actor: str,
) -> None:
    detail = item.detail
    conversion = item.conversion
    item_id = await conn.fetchval(
        """
        INSERT INTO o_booking_item (booking_id, item_seq, item_kind, source_code, title_snapshot, quantity,
            currency, sell_amount, source_currency, source_amount, fx_rate_id, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)
        RETURNING id
        """,
        booking_id, seq, _KIND_OF[type(detail)], item.source, item.title, item.quantity, currency,
        item.sell_amount, *_source_columns(conversion), rate_id, actor)
    if isinstance(detail, HotelStay):
        await conn.execute(
            """
            INSERT INTO o_booking_item_hotel (item_id, hotel_id, hotel_name, room_id, room_name, rate_plan_id,
                supply_code, check_in_date, check_out_date, room_count, adult_count, child_count, cancellation_note)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            item_id, detail.hotel_id, detail.hotel_name, detail.room_id, detail.room_name, detail.rate_plan_id,
            detail.supply_code, detail.check_in, detail.check_out, detail.rooms, detail.adults, detail.children,
            detail.cancellation_note)
    elif isinstance(detail, FlightItinerary):
        await conn.execute(
            "INSERT INTO o_booking_item_flight (item_id, trip_kind, adult_count, child_count, infant_count) "
            "VALUES ($1, $2, $3, $4, $5)",
            item_id, detail.trip_kind, detail.adults, detail.children, detail.infants)
        await conn.executemany(
            """
            INSERT INTO o_booking_item_flight_segment (item_id, segment_seq, journey_seq, carrier_code, flight_no,
                origin_code, destination_code, depart_local, arrive_local, cabin_class)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            [(item_id, n, s.journey, s.carrier, s.flight_no, s.origin, s.destination, s.depart_local,
              s.arrive_local, s.cabin) for n, s in enumerate(detail.segments, start=1)])
    else:
        await conn.execute(
            """
            INSERT INTO o_booking_item_event (item_id, event_id, event_name, listing_id, category_name,
                venue_name, starts_local, tz_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            item_id, detail.event_id, detail.event_name, detail.listing_id, detail.category_name,
            detail.venue_name, detail.starts_local, detail.tz_name)
    await conn.executemany(
        """
        INSERT INTO o_booking_item_traveller (item_id, traveller_seq, traveller_kind, given_name, family_name,
            date_of_birth, nationality_code, email, phone, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
        """,
        [(item_id, n, t.kind, t.given_name, t.family_name, t.date_of_birth, t.nationality_code, t.email,
          t.phone, actor) for n, t in enumerate(item.travellers, start=1)])


async def _log(
    conn: Any, booking_id: int, item_id: int | None, kind: str, before: str | None, after: str | None, actor: str,
) -> None:
    await conn.execute(
        "INSERT INTO o_booking_event (booking_id, item_id, event_kind, from_status, to_status, actor) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        booking_id, item_id, kind, before, after, actor)


_DETAIL_SQL = {
    "HOTEL": "SELECT * FROM o_booking_item_hotel WHERE item_id = $1",
    "FLIGHT": "SELECT * FROM o_booking_item_flight WHERE item_id = $1",
    "EVENT_TICKET": "SELECT * FROM o_booking_item_event WHERE item_id = $1",
}


async def get_booking(conn: Any, code: str, *, buyer: Buyer) -> Booking | None:
    """The buyer's own booking, or None: a code that belongs to someone else reads as not found."""
    row = await conn.fetchrow(
        "SELECT * FROM o_booking WHERE booking_code = $1 AND buyer_channel = $2 AND buyer_ref = $3",
        code, buyer.channel, buyer.ref)
    if row is None:
        return None
    items = []
    for item in await conn.fetch("SELECT * FROM o_booking_item WHERE booking_id = $1 ORDER BY item_seq", row["id"]):
        detail = dict(await conn.fetchrow(_DETAIL_SQL[item["item_kind"]], item["id"]))
        if item["item_kind"] == "FLIGHT":
            detail["segments"] = [dict(s) for s in await conn.fetch(
                "SELECT * FROM o_booking_item_flight_segment WHERE item_id = $1 ORDER BY segment_seq", item["id"])]
        travellers = await conn.fetch(
            "SELECT * FROM o_booking_item_traveller WHERE item_id = $1 ORDER BY traveller_seq", item["id"])
        conversion = None
        if item["fx_rate_id"] is not None:
            rate = await conn.fetchrow(
                "SELECT rate, as_of, provider_code FROM c_fx_rate WHERE id = $1", item["fx_rate_id"])
            conversion = FxConversion(
                item["source_amount"], item["source_currency"], rate["rate"], rate["as_of"], rate["provider_code"])
        items.append(Item(
            id=item["id"], seq=item["item_seq"], kind=item["item_kind"], source=item["source_code"],
            title=item["title_snapshot"], quantity=item["quantity"], sell_amount=item["sell_amount"],
            booking_status=item["booking_status"], fulfilment_status=item["fulfilment_status"],
            supplier_order_no=item["supplier_order_no"], supplier_ref=item["supplier_ref"],
            supplier_confirm_no=item["supplier_confirm_no"], pay_by_at=item["pay_by_at"], version=item["version"],
            detail=detail, travellers=tuple(dict(t) for t in travellers), conversion=conversion))
    trip = await conn.fetchrow(
        "SELECT trip_code, title, starts_on, ends_on, status FROM o_trip WHERE id = $1", row["trip_id"])
    return Booking(
        id=row["id"], code=row["booking_code"],
        buyer=Buyer(row["buyer_channel"], row["buyer_ref"], row["chat_ref"]),
        contact=Contact(row["contact_name"], row["contact_email"], row["contact_phone"]),
        locale=row["locale_code"], currency=row["currency"], status=row["status"],
        total_amount=row["total_amount"], version=row["version"], created_at=row["created_dt"],
        order_mode=row["order_mode"], items=tuple(items), trip=Trip(*trip))


async def list_bookings(conn: Any, buyer: Buyer, *, limit: int = 20) -> list[dict[str, Any]]:
    """The buyer's bookings, newest first: code, status, currency, total and creation time."""
    rows = await conn.fetch(
        """
        SELECT booking_code, status, currency, total_amount, created_dt FROM o_booking
        WHERE buyer_channel = $1 AND buyer_ref = $2 ORDER BY created_dt DESC, id DESC LIMIT $3
        """,
        buyer.channel, buyer.ref, max(1, min(limit, 100)))
    return [dict(row) for row in rows]


_TRIP_ROWS = """
    SELECT t.trip_code, t.title, t.starts_on, t.ends_on, t.status AS trip_status, t.created_dt AS trip_created_dt,
           b.booking_code, b.status, b.currency, b.total_amount, b.created_dt,
           (SELECT string_agg(i.item_kind, ',' ORDER BY i.item_seq) FROM o_booking_item i
             WHERE i.booking_id = b.id) AS item_kinds,
           (SELECT string_agg(i.title_snapshot, ' + ' ORDER BY i.item_seq) FROM o_booking_item i
             WHERE i.booking_id = b.id) AS summary
    FROM o_trip t JOIN o_booking b ON b.trip_id = t.id
"""


def _group_trips(rows: list[Any]) -> list[dict[str, Any]]:
    """Master-detail: each trip once, with its bookings in the order they were placed."""
    trips: dict[str, dict[str, Any]] = {}
    for row in rows:
        trip = trips.setdefault(row["trip_code"], {
            "trip_code": row["trip_code"], "title": row["title"], "starts_on": row["starts_on"],
            "ends_on": row["ends_on"], "status": row["trip_status"], "bookings": []})
        trip["bookings"].append({
            "booking_code": row["booking_code"], "status": row["status"], "currency": row["currency"],
            "total_amount": row["total_amount"], "created_dt": row["created_dt"],
            "kinds": row["item_kinds"].split(","), "summary": row["summary"]})
    return list(trips.values())


async def list_trips(conn: Any, buyer: Buyer, *, limit: int = 10) -> list[dict[str, Any]]:
    """The buyer's trips, newest first, each with its bookings: the master-detail view of what they ordered."""
    rows = await conn.fetch(
        _TRIP_ROWS + """
        WHERE b.buyer_channel = $1 AND b.buyer_ref = $2 AND t.id IN (
            SELECT trip_id FROM o_booking WHERE buyer_channel = $1 AND buyer_ref = $2
            GROUP BY trip_id ORDER BY max(created_dt) DESC LIMIT $3)
        ORDER BY t.created_dt DESC, t.id DESC, b.created_dt, b.id
        """,
        buyer.channel, buyer.ref, max(1, min(limit, 50)))
    return _group_trips(rows)


async def find_customers(
    conn: Any, *, telegram_user_id: str | None = None, email: str | None = None,
) -> list[dict[str, Any]]:
    """Customers by Telegram account id or by email (case-insensitive), each with their trips and bookings.

    For operators only. Never reachable from a chat: an email typed by a stranger must not open someone's orders.
    """
    customers = await conn.fetch(
        """
        SELECT id, telegram_user_id, email, display_name, phone, created_dt FROM o_customer
        WHERE erased_at IS NULL AND (($1::text IS NOT NULL AND telegram_user_id = $1)
                                     OR ($2::text IS NOT NULL AND lower(email) = lower($2)))
        ORDER BY id
        """,
        telegram_user_id, email)
    found = []
    for customer in customers:
        rows = await conn.fetch(
            _TRIP_ROWS + "WHERE t.customer_id = $1 ORDER BY t.created_dt DESC, t.id DESC, b.created_dt, b.id",
            customer["id"])
        found.append({**dict(customer), "trips": _group_trips(rows)})
    return found


async def erase_customer(conn: Any, customer_id: int, *, actor: str) -> None:
    """Erase a person: their customer record, every booking of theirs and the names of their trips.

    The orders, amounts and history stay, linked to a customer row that no longer says who it was.
    """
    for booking_id in [row["id"] for row in await conn.fetch(
            "SELECT id FROM o_booking WHERE customer_id = $1 AND erased_at IS NULL", customer_id)]:
        await _erase_booking(conn, booking_id, actor)
    await conn.execute(
        "UPDATE o_trip SET title = 'Trip', version = version + 1, updated_by = $1 WHERE customer_id = $2",
        actor, customer_id)
    await conn.execute(
        """
        UPDATE o_customer SET telegram_user_id = NULL, email = NULL, display_name = NULL, phone = NULL,
            locale_code = NULL, erased_at = clock_timestamp(), version = version + 1, updated_by = $1
        WHERE id = $2 AND erased_at IS NULL
        """,
        actor, customer_id)


async def move_booking(conn: Any, booking: Booking, to: str, *, actor: str) -> int:
    """Move a booking to status *to*; returns the new version.

    Refused when the move is not allowed from the status the caller read, or when someone else
    changed the booking since (``stale``).
    """
    if to not in BOOKING_MOVES.get(booking.status, ()):
        raise OrderError("bad_move", f"A {booking.status} booking cannot become {to}.")
    version = await conn.fetchval(
        """
        UPDATE o_booking SET status = $1, version = version + 1, updated_by = $2
        WHERE id = $3 AND status = $4 AND version = $5 RETURNING version
        """,
        to, actor, booking.id, booking.status, booking.version)
    if version is None:
        raise OrderError("stale", "The booking changed since it was read.")
    await _log(conn, booking.id, None, "BOOKING_STATUS", booking.status, to, actor)
    return version


async def move_item(
    conn: Any, booking: Booking, item: Item, *, booking_status: str | None = None,
    fulfilment_status: str | None = None, actor: str,
) -> int:
    """Move one item on either or both of its status axes; returns the new version."""
    if booking_status and booking_status not in ITEM_MOVES.get(item.booking_status, ()):
        raise OrderError("bad_move", f"A {item.booking_status} item cannot become {booking_status}.")
    if fulfilment_status and fulfilment_status not in FULFILMENT_MOVES.get(item.fulfilment_status, ()):
        raise OrderError("bad_move", f"Fulfilment cannot go from {item.fulfilment_status} to {fulfilment_status}.")
    if booking_status == "CANCELLED" and (fulfilment_status or item.fulfilment_status) not in _NO_SUPPLIER_ORDER:
        raise OrderError("supplier_order_live", "Cancel the supplier's order before the item.")
    version = await conn.fetchval(
        """
        UPDATE o_booking_item SET booking_status = coalesce($1, booking_status),
            fulfilment_status = coalesce($2, fulfilment_status), version = version + 1, updated_by = $3
        WHERE id = $4 AND booking_id = $5 AND version = $6 RETURNING version
        """,
        booking_status, fulfilment_status, actor, item.id, booking.id, item.version)
    if version is None:
        raise OrderError("stale", "The item changed since it was read.")
    if booking_status:
        await _log(conn, booking.id, item.id, "ITEM_STATUS", item.booking_status, booking_status, actor)
    if fulfilment_status:
        await _log(conn, booking.id, item.id, "ITEM_FULFILMENT", item.fulfilment_status, fulfilment_status, actor)
    return version


async def confirm_simulated(conn: Any, booking: Booking, *, actor: str) -> Booking:
    """Confirm a freshly created simulated order: every item is CONFIRMED with no supplier order, and the booking is
    CONFIRMED. Call it in the transaction that created the booking, so nobody sees it half-confirmed. No supplier
    request is written, which is what makes the order a simulation."""
    if booking.order_mode != "SIMULATED":
        raise OrderError("bad_mode", "Only a simulated order is confirmed without a supplier.")
    if booking.status != "DRAFT":
        raise OrderError("bad_move", f"A {booking.status} booking is not a new order.")
    for item in booking.items:
        await move_item(conn, booking, item, booking_status="CONFIRMED", fulfilment_status="NOT_APPLICABLE",
                        actor=actor)
    await move_booking(conn, booking, "CONFIRMED", actor=actor)
    return await get_booking(conn, booking.code, buyer=booking.buyer)


async def cancel_booking(conn: Any, booking: Booking, *, actor: str) -> Booking:
    """Cancel a confirmed booking whose items no supplier holds an order for. Refused (``supplier_order_live``)
    while a supplier does, since cancelling that order is a supplier call this store does not make."""
    if any(item.fulfilment_status not in _NO_SUPPLIER_ORDER for item in booking.items):
        raise OrderError("supplier_order_live", "A supplier holds an order for this booking.")
    if booking.status != "CONFIRMED":
        raise OrderError("bad_move", f"A {booking.status} booking cannot be cancelled.")
    for item in booking.items:
        await move_item(conn, booking, item, booking_status="CANCELLED", actor=actor)
    await move_booking(conn, booking, "CANCELLED", actor=actor)
    # A trip with no order left is over: it is shown as cancelled and takes no further order.
    await conn.execute(
        """
        UPDATE o_trip SET status = 'CANCELLED', version = version + 1, updated_by = $1
        WHERE id = (SELECT trip_id FROM o_booking WHERE id = $2) AND status = 'ACTIVE'
          AND NOT EXISTS (SELECT 1 FROM o_booking b WHERE b.trip_id = o_trip.id AND b.status <> 'CANCELLED')
        """,
        actor, booking.id)
    return await get_booking(conn, booking.code, buyer=booking.buyer)


async def open_supplier_request(
    conn: Any, booking: Booking, item: Item, kind: str, *, request_sha256: str | None = None, actor: str,
) -> SupplierRequest:
    """Record that a supplier write is about to be sent. Commit this before calling the supplier.

    Refused (``request_pending``) while an earlier attempt of the same kind is NEW or UNKNOWN:
    that one must be settled by reading the supplier first, or the order could be placed twice.
    Refused (``already_placed``) once an attempt of that kind succeeded, and for an erased or
    finished booking. PAY needs a placed order to pay for.
    """
    # The booking row is the lock every writer of this order takes first.
    state = await conn.fetchrow(
        """
        SELECT b.status, b.erased_at, i.booking_status FROM o_booking b
        LEFT JOIN o_booking_item i ON i.booking_id = b.id AND i.id = $2
        WHERE b.id = $1 FOR UPDATE OF b
        """,
        booking.id, item.id)
    if state is None or state["booking_status"] is None:
        raise OrderError("not_this_booking", "The item does not belong to this booking.")
    if state["erased_at"] is not None:
        raise OrderError("erased", "The booking is erased.")
    if kind != "CANCEL" and (state["status"] in _FINAL_BOOKING or state["booking_status"] not in ITEM_MOVES):
        raise OrderError("not_open", "The booking or the item is finished.")
    earlier = {row["state"] for row in await conn.fetch(
        "SELECT state FROM o_supplier_request WHERE item_id = $1 AND request_kind = $2", item.id, kind)}
    if earlier & {"NEW", "UNKNOWN"}:
        raise OrderError("request_pending", "An earlier attempt for this item has no result yet.")
    if "PLACED" in earlier:
        raise OrderError("already_placed", "This was already done for the item.")
    if kind == "PAY" and not await conn.fetchval(
            "SELECT 1 FROM o_supplier_request WHERE item_id = $1 AND request_kind = 'PLACE' AND state = 'PLACED'",
            item.id):
        raise OrderError("not_placed", "There is no placed order to pay for.")
    attempt = 1 + await conn.fetchval(
        "SELECT coalesce(max(attempt_no), 0) FROM o_supplier_request WHERE item_id = $1 AND request_kind = $2",
        item.id, kind)
    key = f"ac:item:{item.id}:{kind.lower()}:{attempt}"
    row = await conn.fetchrow(
        """
        INSERT INTO o_supplier_request (item_id, source_code, request_kind, attempt_no, client_request_key,
            request_sha256, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7) RETURNING id, state, version
        """,
        item.id, item.source, kind, attempt, key, request_sha256, actor)
    await _log(conn, booking.id, item.id, "SUPPLIER_REQUEST", None, "NEW", actor)
    return SupplierRequest(row["id"], item.id, kind, attempt, key, row["state"], row["version"])


async def close_supplier_request(
    conn: Any, booking: Booking, request: SupplierRequest, state: str, *, result_code: str,
    supplier_order_no: str | None = None, supplier_ref: str | None = None, supplier_confirm_no: str | None = None,
    pay_by_at: datetime | None = None, response_sha256: str | None = None, actor: str,
) -> None:
    """Record what the supplier answered: PLACED, FAILED, or UNKNOWN when there was no clear answer.

    The identifiers a PLACED answer carries are copied onto the item, where the order is read from.
    Validate before the supplier is called whatever can be validated: after it has answered, a
    refused write here leaves the attempt NEW, and it then blocks the next one as it should.
    """
    if state not in REQUEST_MOVES.get(request.state, ()):
        raise OrderError("bad_move", f"A {request.state} supplier request cannot become {state}.")
    if not _RESULT_CODE.fullmatch(result_code):
        raise OrderError("bad_result", "A result code is a short machine label, never supplier text.")
    if pay_by_at is not None and pay_by_at.tzinfo is None:
        raise OrderError("bad_time", "A payment deadline is an instant: it needs a time zone.")
    if not await conn.fetchval(
            "SELECT 1 FROM o_booking_item WHERE id = $1 AND booking_id = $2", request.item_id, booking.id):
        raise OrderError("not_this_booking", "The supplier request does not belong to this booking.")
    updated = await conn.fetchval(
        """
        UPDATE o_supplier_request SET state = $1, result_code = $2, supplier_order_no = $3, supplier_ref = $4,
            response_sha256 = $5, version = version + 1, updated_by = $6
        WHERE id = $7 AND state = $8 AND version = $9 RETURNING id
        """,
        state, result_code, supplier_order_no, supplier_ref, response_sha256, actor, request.id, request.state,
        request.version)
    if updated is None:
        raise OrderError("stale", "The supplier request changed since it was read.")
    if state == "PLACED":
        await conn.execute(
            """
            UPDATE o_booking_item SET supplier_order_no = coalesce($1, supplier_order_no),
                supplier_ref = coalesce($2, supplier_ref), supplier_confirm_no = coalesce($3, supplier_confirm_no),
                pay_by_at = coalesce($4, pay_by_at), version = version + 1, updated_by = $5
            WHERE id = $6
            """,
            supplier_order_no, supplier_ref, supplier_confirm_no, pay_by_at, actor, request.item_id)
    await _log(conn, booking.id, request.item_id, "SUPPLIER_REQUEST", request.state, state, actor)


async def erase_personal_data(conn: Any, booking: Booking, *, actor: str) -> None:
    """Erase who bought and who travels, in place and for good; the order itself is kept.

    The buyer's account id becomes ``erased:<booking code>``, so the buyer can no longer look the
    booking up, and its request key is forgotten. Doing it twice changes nothing. The customer master
    still knows the person: ``erase_customer`` erases a person everywhere.
    """
    await _erase_booking(conn, booking.id, actor)


async def _erase_booking(conn: Any, booking_id: int, actor: str) -> None:
    erased = await conn.fetchval(
        """
        UPDATE o_booking SET contact_name = NULL, contact_email = NULL, contact_phone = NULL, chat_ref = NULL,
            idempotency_key = NULL, request_sha256 = NULL, buyer_ref = 'erased:' || booking_code,
            erased_at = clock_timestamp(), version = version + 1, updated_by = $1
        WHERE id = $2 AND erased_at IS NULL RETURNING id
        """,
        actor, booking_id)
    if erased is None:
        return
    await conn.execute(
        """
        UPDATE o_booking_item_traveller SET given_name = NULL, family_name = NULL, date_of_birth = NULL,
            nationality_code = NULL, email = NULL, phone = NULL, erased_at = clock_timestamp(),
            version = version + 1, updated_by = $1
        WHERE erased_at IS NULL AND item_id IN (SELECT id FROM o_booking_item WHERE booking_id = $2)
        """,
        actor, booking_id)
    await _log(conn, booking_id, None, "ERASED", None, None, actor)
