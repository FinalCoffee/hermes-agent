"""What the search tools showed, per caller, under short quote ids.

Every priced thing a search returns that can be ordered (a hotel rate, a flight fare, an event listing) is
remembered here under a quote_id. place_order takes quote ids only, so the facts and the USD price of an order
come from what the caller was actually shown, never from numbers the model retypes.

An entry belongs to the caller it was shown to, expires after half an hour, and lives in this process: a restart
forgets every quote and the caller searches again. The table is shared by the tool threads, so every access is
under one lock, and each caller and the table as a whole are capped, so one caller cannot evict everyone else.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from agent.secret_scope import current_secret_scope_home

from . import _common
from ._common import ApiError
from .fx import Converted

QUOTE_TTL_SECONDS = 30 * 60.0
PER_CALLER_MAX = 50
TOTAL_MAX = 2000
QUOTE_ID_RE = re.compile(r"Q[0-9A-F]{8}")

HOTEL = "HOTEL"
FLIGHT = "FLIGHT"
EVENT_TICKET = "EVENT_TICKET"

_UNKNOWN = ApiError(
    "quote_unknown", "That quote is unknown or has expired. Search again for fresh quote ids.")


@dataclass(frozen=True)
class HotelRate:
    hotel_id: int
    hotel_name: str
    room_id: int | None
    room_name: str
    rate_plan_id: str
    rate_plan_name: str
    supply_code: str | None
    check_in: date
    check_out: date
    adults: int
    rooms: int
    cancellation: str | None
    total: Converted


@dataclass(frozen=True)
class FlightLeg:
    carrier: str
    flight_no: str
    origin: str
    destination: str
    depart_local: datetime
    arrive_local: datetime
    cabin: str | None


@dataclass(frozen=True)
class FlightFare:
    legs: tuple[FlightLeg, ...]
    adults: int
    total: Converted
    price_key: str


@dataclass(frozen=True)
class EventListing:
    event_id: str
    event_name: str
    venue: str | None
    category: str | None
    starts_local: datetime | None
    tz_name: str | None
    listing_id: str
    listing_name: str
    unit_amount: Decimal
    currency: str
    allowed: tuple[int, ...]


@dataclass(frozen=True)
class Quote:
    owner: tuple[str | None, str]
    kind: str
    facts: HotelRate | FlightFare | EventListing
    expires_at: float


_lock = threading.Lock()
_quotes: dict[str, Quote] = {}


def _owner() -> tuple[str | None, str]:
    """Whose quote this is: the sender, within the profile whose search showed it. One process serves many profiles,
    and the same person may talk to two of them, so the profile is part of the owner."""
    return current_secret_scope_home(), _common.caller_id()


def remember(kind: str, facts: HotelRate | FlightFare | EventListing) -> str:
    """A new quote id for what the caller is being shown now. Bound to the caller's identity."""
    owner = _owner()
    now = time.monotonic()
    with _lock:
        for quote_id in [qid for qid, quote in _quotes.items() if quote.expires_at <= now]:
            del _quotes[quote_id]
        quote_id = _new_id()
        _quotes[quote_id] = Quote(owner, kind, facts, now + QUOTE_TTL_SECONDS)
        _enforce_caps(owner)
    return quote_id


def lookup(quote_id: str, *kinds: str) -> Quote:
    """The quote for this caller, of one of *kinds* (any kind when none is given). Another caller's id, an expired
    one and an unknown one all answer the same, so an id reveals nothing about anyone else's search."""
    now = time.monotonic()
    with _lock:
        quote = _quotes.get(quote_id)
    if quote is None or quote.owner != _owner() or quote.expires_at <= now:
        raise _UNKNOWN
    if kinds and quote.kind not in kinds:
        raise _UNKNOWN
    return quote


def reprice(quote_id: str, facts: HotelRate | FlightFare | EventListing | None) -> None:
    """The caller's quote now stands for *facts*: a later order pays what the caller was last shown. None withdraws
    the quote, for a price that can no longer be ordered."""
    quote = lookup(quote_id)
    with _lock:
        if facts is None:
            _quotes.pop(quote_id, None)
        else:
            _quotes[quote_id] = dataclasses.replace(quote, facts=facts)


def parse_id(value: Any) -> str:
    """A well-formed quote id from a tool argument, upper-cased; anything else is an argument error."""
    text = str(value or "").strip().upper()
    if not QUOTE_ID_RE.fullmatch(text):
        raise ValueError("quote_id is the id shown in a search result, such as Q1A2B3C4D.")
    return text


def _new_id() -> str:
    quote_id = "Q" + secrets.token_hex(4).upper()
    while quote_id in _quotes:
        quote_id = "Q" + secrets.token_hex(4).upper()
    return quote_id


def _enforce_caps(owner: tuple[str | None, str]) -> None:
    """Keep this caller within its own cap (its oldest quotes go first), then the table within its cap by evicting
    the oldest quote of the caller holding the most. A flood therefore only ever evicts the flooder."""
    mine = [qid for qid, quote in _quotes.items() if quote.owner == owner]
    for quote_id in mine[: max(0, len(mine) - PER_CALLER_MAX)]:
        del _quotes[quote_id]
    while len(_quotes) > TOTAL_MAX:
        busiest = Counter(quote.owner for quote in _quotes.values()).most_common(1)[0][0]
        victim = next(qid for qid, quote in _quotes.items() if quote.owner == busiest)
        del _quotes[victim]

