"""What the statement of an order says: the document a user can have as a PDF.

The words and numbers of every block of the page, taken from one order as the store holds it: who ordered, who
travels, each item with its dates and amount, the total, and what is known of the demonstration payment.
statement_pdf.py draws them and decides none.

Every order is a demonstration (tools_orders.py), and the statement says so wherever a reader could take it for a
ticket, a booking confirmation or a receipt. It shows what the order holds and nothing a search knew besides: airports by
their codes, as the order keeps them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from . import quotes
from .orderstore import store

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TRIP_KINDS = {quotes.ONE_WAY: "One way", quotes.ROUND_TRIP: "Round trip", quotes.MULTI_CITY: "Multi-city"}
NOTHING = "—"

PAID, UNPAID = "PAID", "UNPAID"
# What the page says of the payment: the words in the status box, its tone, and the two lines under "Payment".
# The last entry stands for an order whose payment page cannot be asked: it may have been paid, so the page
# claims neither, and says only what holds either way.
_PAYMENT = {
    PAID: ("Demonstration · paid", "good", ("Demonstration payment made", "Nothing was charged")),
    UNPAID: ("Demonstration · not paid", "open", ("Not paid", "A demonstration: nothing is charged")),
    None: ("Demonstration order", "open", ("Payment not known", "A demonstration: nothing is charged")),
}


@dataclass(frozen=True)
class Column:
    """A headed block of lines. The first line is the one that stands out."""

    label: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Line:
    """One item of the order: what it is, when, how much of it, and its amount."""

    title: str
    notes: tuple[str, ...]
    dates: str
    quantity: str
    amount: str


@dataclass(frozen=True)
class Statement:
    reference: str
    issued: str
    status: str
    status_tone: str  # "good", "open" or "muted": the colour of the status box
    parties: tuple[Column, Column, Column]
    lines: tuple[Line, ...]
    currency: str
    total: str
    summary: tuple[Column, Column, Column]


# ---- dates and amounts -----------------------------------------------------------------------------
# Months and weekdays are spelled here: strftime follows the process locale, and the page is in English.

def _day(day: dt.date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _span(first: dt.date, last: dt.date) -> str:
    """Two days as one range that names a month and a year once where they are shared."""
    if first == last:
        return _day(first)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{_day(last)}"
    if first.year == last.year:
        return f"{first.day} {_MONTHS[first.month - 1]}–{_day(last)}"
    return f"{_day(first)}–{_day(last)}"


def _amount(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _count(number: int, one: str) -> str:
    return f"{number} {one}" if number == 1 else f"{number} {one}s"


# ---- the items -------------------------------------------------------------------------------------

def _flight_note(segment: dict) -> str:
    depart, arrive = segment["depart_local"], segment["arrive_local"]
    landing = f"{arrive:%H:%M} {segment['destination_code']}"
    if arrive.date() != depart.date():
        landing += f" on {arrive.day} {_MONTHS[arrive.month - 1]}"
    parts = [
        segment["flight_no"],
        f"{_WEEKDAYS[depart.weekday()]} {depart.day} {_MONTHS[depart.month - 1]}",
        f"{depart:%H:%M} {segment['origin_code']} to {landing}",
        (segment.get("cabin_class") or "").replace("_", " ").capitalize(),  # the supplier writes "premium_economy"
    ]
    return " · ".join(part for part in parts if part)


def _flight(item: store.Item) -> Line:
    detail = item.detail
    segments = detail["segments"]
    legs = [[s for s in segments if s["journey_seq"] == number]
            for number in sorted({s["journey_seq"] for s in segments})]
    passengers = detail["adult_count"] + detail["child_count"] + detail["infant_count"]
    return Line(
        title=", ".join(f"{leg[0]['origin_code']} to {leg[-1]['destination_code']}" for leg in legs),
        notes=(_TRIP_KINDS[detail["trip_kind"]], *(_flight_note(segment) for segment in segments)),
        dates=_span(segments[0]["depart_local"].date(), segments[-1]["arrive_local"].date()),
        quantity=_count(passengers, "passenger"), amount=_amount(item.sell_amount))


def _hotel(item: store.Item) -> Line:
    detail = item.detail
    stay = [detail.get("room_name"), _count(detail["room_count"], "room"),
            _count(detail["adult_count"] + detail["child_count"], "guest")]
    return Line(
        title=detail["hotel_name"], notes=(" · ".join(part for part in stay if part),),
        dates=_span(detail["check_in_date"], detail["check_out_date"]),
        quantity=_count((detail["check_out_date"] - detail["check_in_date"]).days, "night"),
        amount=_amount(item.sell_amount))


def _event(item: store.Item) -> Line:
    detail = item.detail
    starts = detail.get("starts_local")
    where = " · ".join(part for part in (detail.get("category_name"), detail.get("venue_name")) if part)
    notes = [where, f"Starts {starts:%H:%M}, local time" if starts else None]
    return Line(
        title=detail["event_name"], notes=tuple(note for note in notes if note),
        dates=_day(starts.date()) if starts else NOTHING,
        quantity=_count(item.quantity, "ticket"), amount=_amount(item.sell_amount))


_LINES = {"FLIGHT": _flight, "HOTEL": _hotel, "EVENT_TICKET": _event}


def _line(item: store.Item) -> Line:
    line = _LINES[item.kind](item)
    if item.conversion is None:
        return line
    source = item.conversion.source_amount
    shown = f"{source:,.0f}" if source == source.to_integral() else f"{source:,.2f}"
    converted = f"Converted from {item.conversion.source_currency} {shown}: an approximate price"
    return Line(line.title, (*line.notes, converted), line.dates, line.quantity, line.amount)


# ---- the people and the summary --------------------------------------------------------------------

def _travellers(booking: store.Booking) -> tuple[str, ...]:
    """Everyone who travels, in the order they were given. The family name comes first, in capitals, as travel
    documents write a name: that reads right whichever order the traveller's language uses.

    The same people are named on each item they travel on, so a name counts as often as the item that holds it
    most: twice for two travellers of one name on a flight, once for one traveller on a flight and a hotel."""
    counts: dict[str, int] = {}
    for item in booking.items:
        on_item: dict[str, int] = {}
        for traveller in item.travellers:
            family, given = traveller.get("family_name"), traveller.get("given_name")
            name = " ".join(part for part in (family.upper() if family else None, given) if part)
            if name:
                on_item[name] = on_item.get(name, 0) + 1
        for name, count in on_item.items():
            counts[name] = max(counts.get(name, 0), count)
    return tuple(name for name, count in counts.items() for _ in range(count))


def _lines_or_nothing(*lines: str | None) -> tuple[str, ...]:
    return tuple(line for line in lines if line) or (NOTHING,)


def build(booking: store.Booking, *, payment: str | None, issued: dt.date) -> Statement:
    """The statement of a confirmed order. *payment* is what the payment page answers for it (PAID, UNPAID), or
    None when the page cannot be asked. *issued* and the day the order was placed are days in UTC, and say so."""
    status, tone, payment_lines = _PAYMENT[payment if payment in (PAID, UNPAID) else None]
    trip = booking.trip
    trip_days = _span(trip.starts_on, trip.ends_on) if trip.starts_on and trip.ends_on else None
    return Statement(
        reference=booking.code,
        issued=f"{_day(issued)} UTC",
        status=status,
        status_tone=tone,
        parties=(
            Column("Ordered by", _lines_or_nothing(booking.contact.name, booking.contact.email, booking.contact.phone)),
            Column("Travellers", _travellers(booking) or (NOTHING,)),
            Column("Booked through", ("Accomy AI Travel", "Demonstration service")),
        ),
        lines=tuple(_line(item) for item in booking.items),
        currency=booking.currency,
        total=_amount(booking.total_amount),
        summary=(
            Column("Payment", payment_lines),
            Column("Order", ("Demonstration order", "Not placed with a supplier",
                             f"Placed {_day(booking.created_at.astimezone(dt.UTC).date())} UTC")),
            Column("Trip", _lines_or_nothing(trip.title, trip_days, f"Trip {trip.code}")),
        ),
    )
