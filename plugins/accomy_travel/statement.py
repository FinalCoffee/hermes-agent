"""What the statement of an order says: the document a user can have as a PDF.

The words and numbers of every block of the page, taken from one order as the store holds it: who ordered, who
travels, each item with its dates and amount, the total, and what is known of the demonstration payment.
statement_pdf.py draws them and decides none.

A statement is written in the language of the conversation that asks for it (statement_text.py holds the words
of each), and in English for any other language.

Every order is a demonstration (tools_orders.py), and the statement says so wherever a reader could take it for a
ticket, a booking confirmation or a receipt. It shows what the order holds and nothing a search knew besides: airports by
their codes, as the order keeps them.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

from . import quotes, statement_text
from .orderstore import store

NOTHING = "—"

PAID, UNPAID = "PAID", "UNPAID"
# What the page says of the payment: the entry of the words that holds the status box and the two lines under
# "Payment", and the tone of the box. The last entry stands for an order whose payment page cannot be asked: it
# may have been paid, so the page claims neither, and says only what holds either way.
_PAYMENT = {PAID: ("paid", "good"), UNPAID: ("unpaid", "open"), None: ("unknown", "open")}
_TRIP_KINDS = {quotes.ONE_WAY: "one_way", quotes.ROUND_TRIP: "round_trip", quotes.MULTI_CITY: "multi_city"}
# The supplier's cabin names, without case, spaces, hyphens and underscores ("premium_economy", "PremiumEconomy").
_CABIN_SEPARATORS_RE = re.compile(r"[\s_-]+")


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
class Page:
    """What every statement says around its order, in the statement's language: the headings, the notice that
    the order is a demonstration, what the document is and is not, and the footer."""

    document: str
    title: str
    reference: str
    issued: str  # the whole line, with the day in it
    description: str
    dates: str
    quantity: str
    amount: str
    subtotal: str
    total: str
    not_charged: str
    banner: str
    explanation: tuple[tuple[str, str], ...]  # (what stands out, the sentence after it)
    lead_gap: str  # what lies between those two: a space, or nothing after full-width punctuation
    footer: str
    page_number: str  # holds {n}
    pdf_title: str
    pdf_subject: str


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
    page: Page


# ---- dates and amounts -----------------------------------------------------------------------------

def _dated(words: dict, template: str, day: dt.date) -> str:
    months = words["months"]
    return words[template].format(
        y=day.year, m=day.month, d=day.day, mon=months[day.month - 1] if months else "",
        wd=words["weekdays"][day.weekday()])


def _day(words: dict, day: dt.date) -> str:
    return _dated(words, "day", day)


def _span(words: dict, first: dt.date, last: dt.date) -> str:
    """Two days as one range that names a month and a year once where they are shared."""
    if first == last:
        return _day(words, first)
    parts = {"day1": _day(words, first), "day2": _day(words, last), "d1": first.day, "d2": last.day,
             "short1": _dated(words, "short_day", first), "short2": _dated(words, "short_day", last)}
    if (first.year, first.month) == (last.year, last.month):
        return words["span_in_month"].format(**parts)
    if first.year == last.year:
        return words["span_in_year"].format(**parts)
    return f"{parts['day1']}–{parts['day2']}"


def _amount(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _count(words: dict, kind: str, number: int) -> str:
    one, many = words[kind]
    return (one if number == 1 else many).format(n=number)


# ---- the items -------------------------------------------------------------------------------------

def _cabin(words: dict, name: str | None) -> str:
    """The cabin in the statement's language; a name the words do not hold reads as the supplier wrote it."""
    name = name or ""
    known = words["cabins"].get(_CABIN_SEPARATORS_RE.sub("", name.lower()))
    return known or name.replace("_", " ").capitalize()  # the supplier writes "premium_economy"


def _flight_note(words: dict, segment: dict) -> str:
    depart, arrive = segment["depart_local"], segment["arrive_local"]
    later = arrive.date() != depart.date()
    parts = [
        segment["flight_no"],
        _dated(words, "weekday_day", depart.date()),
        words["flight_times"].format(
            departs=f"{depart:%H:%M}", origin=segment["origin_code"], arrives=f"{arrive:%H:%M}",
            destination=segment["destination_code"],
            later=words["later"].format(day=_dated(words, "short_day", arrive.date())) if later else ""),
        _cabin(words, segment.get("cabin_class")),
    ]
    return " · ".join(part for part in parts if part)


def _flight(words: dict, item: store.Item) -> Line:
    detail = item.detail
    segments = detail["segments"]
    legs = [[s for s in segments if s["journey_seq"] == number]
            for number in sorted({s["journey_seq"] for s in segments})]
    passengers = detail["adult_count"] + detail["child_count"] + detail["infant_count"]
    return Line(
        title=", ".join(words["route"].format(origin=leg[0]["origin_code"], destination=leg[-1]["destination_code"])
                        for leg in legs),
        notes=(words[_TRIP_KINDS[detail["trip_kind"]]], *(_flight_note(words, segment) for segment in segments)),
        dates=_span(words, segments[0]["depart_local"].date(), segments[-1]["arrive_local"].date()),
        quantity=_count(words, "passengers", passengers), amount=_amount(item.sell_amount))


def _hotel(words: dict, item: store.Item) -> Line:
    detail = item.detail
    stay = [detail.get("room_name"), _count(words, "rooms", detail["room_count"]),
            _count(words, "guests", detail["adult_count"] + detail["child_count"])]
    return Line(
        title=detail["hotel_name"], notes=(" · ".join(part for part in stay if part),),
        dates=_span(words, detail["check_in_date"], detail["check_out_date"]),
        quantity=_count(words, "nights", (detail["check_out_date"] - detail["check_in_date"]).days),
        amount=_amount(item.sell_amount))


def _event(words: dict, item: store.Item) -> Line:
    detail = item.detail
    starts = detail.get("starts_local")
    where = " · ".join(part for part in (detail.get("category_name"), detail.get("venue_name")) if part)
    notes = [where, words["starts"].format(time=f"{starts:%H:%M}") if starts else None]
    return Line(
        title=detail["event_name"], notes=tuple(note for note in notes if note),
        dates=_day(words, starts.date()) if starts else NOTHING,
        quantity=_count(words, "tickets", item.quantity), amount=_amount(item.sell_amount))


_LINES = {"FLIGHT": _flight, "HOTEL": _hotel, "EVENT_TICKET": _event}


def _line(words: dict, item: store.Item) -> Line:
    line = _LINES[item.kind](words, item)
    if item.conversion is None:
        return line
    source = item.conversion.source_amount
    shown = f"{source:,.0f}" if source == source.to_integral() else f"{source:,.2f}"
    converted = words["converted"].format(currency=item.conversion.source_currency, amount=shown)
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


def page(language: str | None, issued: str) -> Page:
    """What every statement in *language* says around its order. *issued* is the day it is made, as written."""
    words = statement_text.words(language)
    return Page(
        document=words["document"], title=words["title"], reference=words["reference"],
        issued=words["issued"].format(day=issued),
        description=words["description"], dates=words["dates"], quantity=words["quantity"], amount=words["amount"],
        subtotal=words["subtotal"], total=words["total"], not_charged=words["not_charged"], banner=words["banner"],
        explanation=(words["what_it_is"], words["what_it_is_not"]), lead_gap=words["lead_gap"],
        footer=words["footer"], page_number=words["page"], pdf_title=words["pdf_title"],
        pdf_subject=words["pdf_subject"])


def build(booking: store.Booking, *, payment: str | None, issued: dt.date, language: str | None = None) -> Statement:
    """The statement of a confirmed order, written in *language*: English when that is not a language of
    statement_text.py. *payment* is what the payment page answers for it (PAID, UNPAID), or None when the page
    cannot be asked. *issued* and the day the order was placed are days in UTC, and say so."""
    words = statement_text.words(language)
    said, tone = _PAYMENT[payment if payment in (PAID, UNPAID) else None]
    status, *payment_lines = words[said]
    trip = booking.trip
    trip_days = _span(words, trip.starts_on, trip.ends_on) if trip.starts_on and trip.ends_on else None
    issued_day = words["utc"].format(day=_day(words, issued))
    placed = words["utc"].format(day=_day(words, booking.created_at.astimezone(dt.UTC).date()))
    return Statement(
        reference=booking.code,
        issued=issued_day,
        status=status,
        status_tone=tone,
        parties=(
            Column(words["ordered_by"],
                   _lines_or_nothing(booking.contact.name, booking.contact.email, booking.contact.phone)),
            Column(words["travellers"], _travellers(booking) or (NOTHING,)),
            Column(words["booked_through"], ("Accomy AI Travel", words["service"])),
        ),
        lines=tuple(_line(words, item) for item in booking.items),
        currency=booking.currency,
        total=_amount(booking.total_amount),
        summary=(
            Column(words["payment"], tuple(payment_lines)),
            Column(words["order"], (*words["order_lines"], words["placed"].format(day=placed))),
            Column(words["trip"], _lines_or_nothing(trip.title, trip_days, words["trip_code"].format(code=trip.code))),
        ),
        page=page(language, issued_day),
    )
